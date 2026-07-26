import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, MultiFernet
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import ApiKey

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


def authenticate_bearer(token: str) -> ApiKey:
    """OIDC Access Token 검증 → ApiKey 형태로 매핑 (name=preferred_username, admin=롤 매핑)."""
    import jwt  # noqa: PLC0415

    settings = get_settings()
    if not settings.oidc_issuer:
        raise HTTPException(status_code=401, detail="OIDC not configured")
    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
        options = {"verify_aud": bool(settings.oidc_audience)}
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience or None,
            options=options,
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid bearer token: {e}")

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
        return authenticate_bearer(authorization[7:].strip())
    if not x_api_key:
        raise HTTPException(status_code=401, detail="x-api-key header required")
    if settings.admin_api_key and hmac.compare_digest(x_api_key, settings.admin_api_key):
        return ApiKey(name="bootstrap-admin", key_hash="", is_admin=True)
    row = db.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(x_api_key))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="invalid api key")
    return row


def require_admin(key: ApiKey = Depends(require_api_key)) -> ApiKey:
    if not key.is_admin:
        raise HTTPException(status_code=403, detail="admin key required")
    return key
