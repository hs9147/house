"""paas 자체 OIDC Provider — Keycloak 같은 별도 IdP 없이 paas의 UserAccount 계정을
그대로 SSO ID("paas ID")로 쓰기 위한 최소 구현.

Authorization Code 플로우만 지원한다(Gitea 등 외부 서비스가 필요로 하는 전부):
  1. GET  /paas/oauth2/authorize — 로그인 안 돼 있으면 콘솔 로그인 화면으로 보내고,
     돼 있으면(paas_session 쿠키) 1회용 code를 만들어 클라이언트의 redirect_uri로 보낸다.
  2. POST /paas/oauth2/token     — code를 id_token(JWT)으로 교환한다.
  3. GET  /paas/oauth2/jwks      — 그 JWT를 검증할 공개키.
  4. GET  /paas/.well-known/openid-configuration — 위 세 URL을 알려주는 표준 디스커버리 문서.

발급하는 JWT의 클레임 모양(`realm_access.roles`, `preferred_username`)은 기존
security.authenticate_bearer가 Keycloak 토큰에서 읽던 것과 똑같다 — oidc_issuer를
paas 자신의 주소로 맞추기만 하면 그 검증 코드는 한 글자도 안 바꿔도 된다.
"""
import json
import secrets
import time
from datetime import timedelta, timezone
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import OidcAuthCode, UserAccount, utcnow
from ..security import hash_key

AUTH_CODE_TTL = timedelta(seconds=60)  # 발급 즉시 교환되는 값이라 짧게 둔다
ID_TOKEN_TTL = timedelta(minutes=15)


class OidcProviderError(RuntimeError):
    """클라이언트/redirect_uri 오류 등 — 발급자가 열려 있지 않은 곳으로는 리다이렉트
    하지 않고 여기서 바로 400/401로 끝낸다(open redirect 방지)."""


_signing_key: rsa.RSAPrivateKey | None = None
_kid: str | None = None


def _load_or_create_signing_key() -> rsa.RSAPrivateKey:
    """서명 키를 파일에서 읽거나, 없으면 새로 만들어 저장한다.

    재시작해도 같은 키를 써야 한다 — 매번 새로 만들면 이미 나눠준 JWKS 공개키로
    검증하려는 클라이언트(Gitea 등)의 기존 세션이 재시작 한 번에 전부 깨진다.
    """
    path: Path = get_settings().oidc_provider_signing_key_path
    if path.is_file():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return key


def _get_signing_key() -> rsa.RSAPrivateKey:
    global _signing_key
    if _signing_key is None:
        _signing_key = _load_or_create_signing_key()
    return _signing_key


def _b64url_uint(n: int) -> str:
    import base64
    byte_length = (n.bit_length() + 7) // 8 or 1
    return base64.urlsafe_b64encode(n.to_bytes(byte_length, "big")).rstrip(b"=").decode("ascii")


def key_id() -> str:
    """공개키 자체에서 결정되는 안정적인 kid — 별도로 저장하지 않아도 키가 바뀌면 같이 바뀐다."""
    global _kid
    if _kid is None:
        pub = _get_signing_key().public_key().public_numbers()
        _kid = hash_key(f"{pub.n}:{pub.e}")[:16]
    return _kid


def jwks() -> dict:
    pub = _get_signing_key().public_key().public_numbers()
    return {"keys": [{
        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": key_id(),
        "n": _b64url_uint(pub.n), "e": _b64url_uint(pub.e),
    }]}


def issuer() -> str:
    settings = get_settings()
    if settings.oidc_issuer:
        return settings.oidc_issuer.rstrip("/")
    return f"{settings.platform_public_url.rstrip('/')}/paas"


def discovery_document() -> dict:
    iss = issuer()
    return {
        "issuer": iss,
        "authorization_endpoint": f"{iss}/oauth2/authorize",
        "token_endpoint": f"{iss}/oauth2/token",
        "userinfo_endpoint": f"{iss}/oauth2/userinfo",
        "jwks_uri": f"{iss}/oauth2/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "claims_supported": ["sub", "email", "preferred_username", "realm_access"],
    }


def get_clients() -> dict[str, dict]:
    """oidc_provider_clients(JSON 문자열) 파싱 — 형식이 잘못돼도 기동 자체는 막지 않는다."""
    raw = get_settings().oidc_provider_clients
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _find_client(client_id: str) -> dict:
    client = get_clients().get(client_id)
    if client is None:
        raise OidcProviderError(f"unknown client_id: {client_id}")
    return client


def validate_authorize_request(client_id: str, redirect_uri: str, response_type: str) -> None:
    """authorize 단계 검증 — 여기서 걸리면 redirect_uri로도 돌려보내지 않는다(그 URI
    자체가 아직 신뢰할 수 있는지 모르는 단계이므로 open redirect가 될 수 있다)."""
    if response_type != "code":
        raise OidcProviderError(f"unsupported response_type: {response_type}")
    client = _find_client(client_id)
    if redirect_uri not in client.get("redirect_uris", []):
        raise OidcProviderError(f"redirect_uri not registered for {client_id}: {redirect_uri}")


def login_redirect_url(next_path_and_query: str) -> str:
    settings = get_settings()
    base = settings.oidc_provider_login_url or f"{settings.platform_public_url.rstrip('/')}/console/#/login"
    from urllib.parse import quote
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}next={quote(next_path_and_query, safe='')}"


def issue_auth_code(db: Session, client_id: str, email: str, redirect_uri: str, nonce: str = "") -> str:
    code = secrets.token_urlsafe(32)
    db.add(OidcAuthCode(
        code_hash=hash_key(code), client_id=client_id, email=email,
        redirect_uri=redirect_uri, nonce=nonce, expires_at=utcnow() + AUTH_CODE_TTL,
    ))
    db.commit()
    return code


def consume_auth_code(db: Session, code: str, client_id: str, redirect_uri: str) -> tuple[str, str]:
    """code를 1회 소비하고 (email, nonce)를 반환한다. 유효하지 않으면 예외."""
    row = db.execute(
        select(OidcAuthCode).where(OidcAuthCode.code_hash == hash_key(code))
    ).scalar_one_or_none()
    if row is None:
        raise OidcProviderError("invalid or already-used code")
    # 조회 즉시(만료·불일치 여부와 무관하게) 지운다 — 재사용 시도를 항상 막는다.
    email, nonce, matched = row.email, row.nonce, (
        row.client_id == client_id and row.redirect_uri == redirect_uri
    )
    expires = row.expires_at
    if expires.tzinfo is None:  # SQLite는 tz를 보존하지 않음 (security.require_api_key와 동일)
        expires = expires.replace(tzinfo=timezone.utc)
    expired = expires <= utcnow()
    db.delete(row)
    db.commit()
    if expired:
        raise OidcProviderError("code expired")
    if not matched:
        raise OidcProviderError("client_id/redirect_uri mismatch")
    return email, nonce


def issue_id_token(email: str, client_id: str, nonce: str = "") -> str:
    """authenticate_bearer가 읽는 클레임 모양(realm_access.roles, preferred_username)과
    똑같이 만든다 — Keycloak 토큰이든 이걸로 발급한 토큰이든 검증 코드는 하나다.

    preferred_username은 이메일 그 자체로 채운다 — UserAccount.email/UserSession.email이
    이 플랫폼 전체에서 계정을 가리키는 키이므로, authenticate_bearer가 이걸 ApiKey.name에
    옮겨 담아도(/auth/me 등이 UserAccount.email과 맞춰보는 곳들과) 계속 일치한다."""
    settings = get_settings()
    from ..db import SessionLocal  # noqa: PLC0415
    is_admin = False
    db_session = SessionLocal()
    try:
        user = db_session.execute(
            select(UserAccount).where(UserAccount.email == email)
        ).scalar_one_or_none()
        if user is not None:
            is_admin = user.is_admin
    finally:
        db_session.close()

    now = int(time.time())
    payload = {
        "iss": issuer(),
        "sub": email,
        "aud": client_id,
        "iat": now,
        "exp": now + int(ID_TOKEN_TTL.total_seconds()),
        "email": email,
        "preferred_username": email,
        "realm_access": {"roles": [settings.oidc_admin_role] if is_admin else []},
    }
    if nonce:
        payload["nonce"] = nonce
    key_pem = _get_signing_key().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jwt.encode(payload, key_pem, algorithm="RS256", headers={"kid": key_id()})


def exchange_client_credentials(client_id: str, client_secret: str) -> None:
    client = _find_client(client_id)
    if not secrets.compare_digest(client.get("secret", ""), client_secret):
        raise OidcProviderError("invalid client credentials")


def decode_id_token(token: str) -> dict:
    """우리가 발급한 토큰 검증(userinfo 등에서 씀) — 외부 issuer용인 authenticate_bearer와
    달리 JWKS를 네트워크로 가져올 필요 없이 우리 공개키로 바로 검증한다."""
    pub_pem = _get_signing_key().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    try:
        return jwt.decode(
            token, pub_pem, algorithms=["RS256"], issuer=issuer(),
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as e:
        raise OidcProviderError(f"invalid token: {e}") from e


def email_for_session(db: Session, session_token: str) -> str | None:
    """paas_session 쿠키 값을 UserSession에서 조회한다 — security.require_api_key의
    UserSession 조회 분기와 같은 규칙(만료 시 폐기)."""
    if not session_token:
        return None
    from ..models import UserSession  # noqa: PLC0415

    session = db.execute(
        select(UserSession).where(UserSession.token_hash == hash_key(session_token))
    ).scalar_one_or_none()
    if session is None:
        return None
    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= utcnow():
        db.delete(session)
        db.commit()
        return None
    return session.email
