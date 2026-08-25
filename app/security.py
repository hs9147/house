import hashlib
import hmac
import secrets
from datetime import timezone

from cryptography.fernet import Fernet, MultiFernet
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import ApiKey, Project, UserAccount, UserSession, utcnow

_fernet: Fernet | None = None


def _load_key_from_openbao() -> str:
    """OpenBao KV v2에서 Fernet 키 로드 (갭5). 실패는 명확한 에러 — 임시 키 침묵 생성 금지."""
    import httpx  # noqa: PLC0415

    settings = get_settings()
    url = f"{settings.openbao_url.rstrip('/')}/v1/{settings.openbao_key_path.strip('/')}"
    try:
        res = httpx.get(url, headers={"X-Vault-Token": settings.openbao_token}, timeout=10)
    except Exception as e:
        raise RuntimeError(f"OpenBao 연결 실패: {e}") from e
    if res.status_code != 200:
        raise RuntimeError(f"OpenBao 키 조회 실패 (HTTP {res.status_code}): {settings.openbao_key_path}")
    key = res.json().get("data", {}).get("data", {}).get("key", "")
    if not key:
        raise RuntimeError(f"OpenBao 응답에 data.data.key 없음: {settings.openbao_key_path}")
    return key


def get_fernet() -> MultiFernet:
    """암호화는 첫 키(현행), 복호화는 구 키까지 시도 — 키 회전(후속2) 지원."""
    global _fernet
    if _fernet is None:
        settings = get_settings()
        if settings.openbao_url:
            key = _load_key_from_openbao()
        else:
            key = settings.fernet_key
            if not key:
                # 운영에서는 PAAS_FERNET_KEY 고정 또는 OpenBao 사용 — 미설정 시 재기동마다 복호화 불가
                key = Fernet.generate_key().decode()
        keys = [Fernet(key.encode())]
        for old in settings.fernet_keys_old.split(","):
            old = old.strip()
            if old:
                keys.append(Fernet(old.encode()))
        _fernet = MultiFernet(keys)
    return _fernet


def rotate_token(token: str) -> str:
    """구 키로 암호화된 토큰을 현행 키로 재암호화."""
    return get_fernet().rotate(token.encode()).decode()


def encrypt_value(plain: str) -> str:
    return get_fernet().encrypt(plain.encode()).decode()


def decrypt_value(token: str) -> str:
    return get_fernet().decrypt(token.encode()).decode()


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_key() -> str:
    return "paas_" + secrets.token_urlsafe(32)


# --- 비밀번호 (사람이 정한 값) ---
#
# API 키는 256비트 난수라 sha256으로 충분하지만, 비밀번호는 추측 가능한 공간에 있어서
# 빠른 해시로는 못 지킨다. 솔트 + 메모리 하드 KDF가 필요하다. scrypt는 표준 라이브러리에
# 있어 폐쇄망 설치에 의존성을 늘리지 않는다(argon2를 쓰려면 argon2-cffi 추가 필요).
_SCRYPT_N = 2 ** 14  # 약 16MiB, 한 번 검증에 수십 ms
_SCRYPT_R = 8
_SCRYPT_P = 1


def hash_password(plain: str) -> str:
    """scrypt$n$r$p$salt$hash — 파라미터를 함께 저장해 나중에 세기를 올릴 수 있다."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(plain.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            plain.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def issue_session_token() -> str:
    """로그인 세션 토큰 — 비밀번호에서 유도되지 않는 난수라 폐기·만료할 수 있다."""
    return "paass_" + secrets.token_urlsafe(32)


def verify_webhook_signature(secret: str, body: bytes, signature: str) -> bool:
    """GitHub(X-Hub-Signature-256: 'sha256=<hex>') / Gitea(X-Gitea-Signature: '<hex>') 공용."""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    received = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


# --- OIDC (Keycloak 호환) — Bearer JWT 검증. API 키 체계와 병행 ---

_jwk_client = None  # PyJWKClient — JWKS 캐시 내장


def _get_jwk_client():
    global _jwk_client
    if _jwk_client is None:
        import jwt  # noqa: PLC0415

        settings = get_settings()
        jwks_url = settings.oidc_jwks_url or (
            settings.oidc_issuer.rstrip("/") + "/protocol/openid-connect/certs"
        )
        _jwk_client = jwt.PyJWKClient(jwks_url)
    return _jwk_client


def _issued_by_our_own_provider(token: str) -> bool:
    """이 토큰이 내장 OIDC Provider가 서명한 것인지 — 토큰 헤더의 kid로 판별한다.

    주소(issuer 설정값)로 비교하지 않는다. oidc_issuer는 "우리가 신뢰하는 발급자"고
    내장 Provider의 주소와는 별개라, 외부 Keycloak을 쓰면서 Provider도 켜 둔 구성에서
    둘을 같다고 보면 Keycloak 토큰을 우리 키로 검증하려 들어 전부 실패한다.

    헤더는 서명 검증 전 값이라 위조할 수 있지만, 여기서는 **어느 키로 검증할지 고르는
    데만** 쓴다 — kid를 우리 것으로 위조해도 이어지는 서명 검증에서 떨어진다(실패 방향).
    """
    settings = get_settings()
    if not settings.oidc_provider_enabled:
        return False
    import jwt  # noqa: PLC0415

    from .services import oidc_provider  # noqa: PLC0415 — 순환 import 회피

    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return False
    return bool(kid) and kid == oidc_provider.key_id()


def _verification_key_and_issuer(token: str) -> tuple[object, str]:
    """이 토큰을 검증할 (키, 기대 발급자).

    우리가 서명한 토큰이면 개인키가 이미 디스크에 있으므로 그 공개키를 바로 쓴다 —
    JWKS를 HTTP로 가져오지 않는다. 자기 자신을 공개 주소로 다시 호출하면:
      - 그 주소의 서버 인증서가 안 맞으면(사설 CA·자체 서명·내부 DNS로 다른 호스트에
        연결) TLS 검증에서 실패한다 — "인증서가 일치하지 않는다"는 그 오류다.
      - 기본 JWKS 경로가 Keycloak 규약(/protocol/openid-connect/certs)이라 우리
        엔드포인트(/oauth2/jwks)와 달라 404가 난다.
      - 워커가 자기 요청을 처리하는 도중 자기에게 동기 HTTP 호출을 건다.

    기대 발급자도 함께 정한다. 우리 토큰은 **우리가 발급에 쓴 값**(oidc_provider.issuer())
    으로 검증해야 한다 — oidc_issuer로 검증하면, 백채널 분리처럼 발급 주소가 따로 정해지는
    구성에서 발급값과 검증값이 조용히 어긋나 우리 토큰이 전부 401이 된다.
    외부 발급자(Keycloak 등)일 때만 JWKS를 조회하고 oidc_issuer로 검증한다.
    """
    from .services import oidc_provider  # noqa: PLC0415

    if _issued_by_our_own_provider(token):
        return oidc_provider.public_key_pem(), oidc_provider.issuer()

    settings = get_settings()
    if not settings.oidc_issuer:
        raise HTTPException(status_code=401, detail="OIDC not configured")
    return _get_jwk_client().get_signing_key_from_jwt(token).key, settings.oidc_issuer


def validate_email_domain(email: str) -> bool:
    """PAAS_ALLOWED_EMAIL_DOMAIN 환경변수(기본값: cho-fam.com) 기준 이메일 도메인 검증."""
    allowed = get_settings().allowed_email_domain.strip().lower().lstrip("@")
    if not allowed:
        return True
    if "@" not in email:
        return False
    domain = email.strip().lower().split("@")[-1]
    return domain == allowed or domain.endswith("." + allowed)


def authenticate_bearer(token: str) -> ApiKey:
    """OIDC Access Token 검증 → ApiKey 형태로 매핑 (name=preferred_username, admin=롤 매핑)."""
    import jwt  # noqa: PLC0415

    settings = get_settings()
    # 내장 Provider가 켜져 있으면 우리 토큰은 oidc_issuer 설정 없이도 받는다 —
    # 발급·검증 모두 oidc_provider.issuer()라는 한 값으로 닫힌다.
    key, expected_issuer = _verification_key_and_issuer(token)
    try:
        options = {"verify_aud": bool(settings.oidc_audience)}
        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=expected_issuer,
            audience=settings.oidc_audience or None,
            options=options,
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid bearer token: {e}")

    email = payload.get("email", "")
    if settings.allowed_email_domain and email and not validate_email_domain(email):
        raise HTTPException(
            status_code=403,
            detail=f"only @{settings.allowed_email_domain} email domain accounts are allowed",
        )

    roles = set(payload.get("realm_access", {}).get("roles", []))
    name = payload.get("preferred_username") or payload.get("sub", "oidc-user")
    return ApiKey(name=name, key_hash="", is_admin=settings.oidc_admin_role in roles)


def require_api_key(
    x_api_key: str = Header(default=""),
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> ApiKey:
    settings = get_settings()
    if not x_api_key and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        # JWT(헤더.페이로드.서명)면 OIDC 액세스 토큰이다. 그 모양이 아니면 발급 키·세션
        # 토큰을 Bearer로 실어 보낸 것으로 본다 — MCP 규약이 자격증명을
        # `Authorization: Bearer`로 싣기 때문이다(services/mcp_client.py). 사내 MCP
        # 서버(api/mcp_servers.py)는 그 요청을 받는 쪽이라, 여기서 갈라 두지 않으면
        # 플랫폼의 MCP 클라이언트가 플랫폼의 MCP 서버에 401로 막힌다(실측 확인).
        # (플랫폼이 만드는 키에는 점이 없다 — secrets.token_urlsafe.)
        if token.count(".") == 2:
            return authenticate_bearer(token)
        x_api_key = token
    if not x_api_key:
        raise HTTPException(status_code=401, detail="x-api-key header required")
    key = resolve_token(db, x_api_key)
    if key is None:
        raise HTTPException(status_code=401, detail="invalid api key")
    return key


def resolve_token(db: Session, token: str) -> ApiKey | None:
    """토큰 하나를 주체로 해석한다. 통하지 않으면 None.

    require_api_key와 WebSocket 인증(api/system.py의 PowerShell 터미널)이 **같은** 판정을
    쓰기 위해 따로 뺐다 — 인증 경로가 둘로 갈리면 한쪽만 고쳐 놓고 뚫린 채로 남는다.
    """
    settings = get_settings()
    # 1. 관리자 API 키 일치 여부 검증
    if settings.admin_api_key and hmac.compare_digest(token, settings.admin_api_key):
        return ApiKey(name="bootstrap-admin", key_hash="", is_admin=True)

    # 2. DB hash_key 일치 검증
    row = db.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(token))).scalar_one_or_none()
    if row is not None:
        return row

    # 3. 로그인 세션 토큰 (사람 계정) — 만료된 토큰은 통과시키지 않는다.
    session = db.execute(
        select(UserSession).where(UserSession.token_hash == hash_key(token))
    ).scalar_one_or_none()
    if session is not None:
        expires = session.expires_at
        if expires.tzinfo is None:  # SQLite는 tz를 보존하지 않음 (services/preview.py와 동일)
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= utcnow():
            db.delete(session)
            db.commit()
            return None
        return ApiKey(name=session.email, key_hash="", is_admin=session.is_admin)

    # 계정명·길이 기반 인정은 두지 않는다 — 계정명은 감사 로그와 콘솔에 그대로 노출되는
    # 공개 식별자라 비밀값이 아니다. 인증은 관리자 키(1), 발급된 키의 해시(2),
    # 로그인 세션 토큰(3)으로만 성립한다.
    return None


def require_admin(key: ApiKey = Depends(require_api_key)) -> ApiKey:
    if not key.is_admin:
        raise HTTPException(status_code=403, detail="admin key required")
    return key


def viewer_org_ids(db: Session, key: ApiKey) -> set[int]:
    """git_url 등 리포 위치 정보의 노출 범위를 정하기 위한 사용자 소속 조직 id 집합.

    UserAccount 기반 로그인(계정)에만 의미가 있다 — 순수 발급 API 키(name이 계정
    이메일과 무관한 서비스 키)는 조직 개념이 없어 빈 집합(= 전역 프로젝트만 조회
    가능)으로 떨어진다.
    """
    user = db.execute(select(UserAccount).where(UserAccount.email == key.name)).scalar_one_or_none()
    return {o.id for o in user.organizations} if user else set()


def can_view_git_url(project: Project, key: ApiKey, org_ids: set[int]) -> bool:
    """git_url(리포 위치)을 볼 수 있는지 — 관리자, 전역 프로젝트(조직 미지정),
    또는 그 프로젝트 조직 소속 사용자만. 나머지에는 마스킹한다."""
    return key.is_admin or project.organization_id is None or project.organization_id in org_ids
