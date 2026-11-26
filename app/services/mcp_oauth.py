"""GPAX 계정 로그인으로 MCP 연결 — 개인 토큰을 설정 파일에 붙여 넣는 대신.

MCP 권한 규약(OAuth 2.1)을 따른다. 클라이언트(Claude Code·VS Code·Cursor…)가 하는 일:
  1. 토큰 없이 MCP를 부른다 → 401과 `WWW-Authenticate: Bearer resource_metadata=…`
  2. 그 주소(보호 자원 메타데이터, RFC 9728)에서 발급자를 알아낸다
  3. 발급자 메타데이터(RFC 8414)에서 등록·로그인·토큰 주소를 읽는다
  4. 스스로 클라이언트를 등록한다(RFC 7591 — 비밀 없는 공개 클라이언트)
  5. 브라우저로 로그인 창을 연다(PKCE S256, resource=MCP 주소, RFC 8707)
  6. 돌아온 code를 액세스 토큰(1시간)·갱신 토큰(쓸 때마다 바뀜)으로 바꾼다

**Gitea SSO와 같은 길을 탄다** — 로그인은 콘솔 로그인(paas_session 쿠키), 서명은 같은 키
(services/oidc_provider.py), code는 같은 규칙(해시만·60초·1회). 다른 점은 Gitea가 설정에
적힌 기밀 클라이언트인 반면 여기는 누구나 등록하는 공개 클라이언트라는 것 — 그래서:
  - 비밀 대신 PKCE로 code를 그 클라이언트에 묶는다
  - 로그인돼 있어도 바로 code를 주지 않고 **승인 화면**을 거친다(누가·어디로 돌아가는지 보임)
  - 발급자 주소를 따로 둔다(`…/paas/oauth`) — Gitea용 디스커버리는 백채널(localhost)을
    가리키므로 MCP 클라이언트가 그걸 집으면 토큰 교환이 사용자 PC로 간다

액세스 토큰은 **MCP 전용**이다. 발급자가 달라 require_api_key(콘솔·배포 API)는 이 토큰을
받지 않는다 — 개인 MCP 토큰과 같은 울타리.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta, timezone
from urllib.parse import urlencode, urlparse

import jwt
from cryptography.hazmat.primitives import serialization
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import McpOAuthClient, McpOAuthCode, McpOAuthGrant, UserAccount, utcnow
from ..security import MCP_TOKEN_TTL, hash_key
from . import oidc_provider

SCOPE = "mcp"
ACCESS_TOKEN_TTL = timedelta(hours=1)
CODE_TTL = timedelta(seconds=60)
GRANT_TTL = MCP_TOKEN_TTL  # 개인 MCP 토큰과 같은 90일 — 그 뒤엔 다시 로그인한다
CONSENT_TTL = timedelta(minutes=10)
ACCESS_TOKEN_TYP = "at+jwt"  # RFC 9068 — 다른 JWT(id_token)와 섞이지 않게 헤더로 구분한다
API_PATH = "/paas/api/v1"
_LOOPBACK = {"localhost", "127.0.0.1", "[::1]", "::1"}
_FORBIDDEN_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "ws", "wss"}
_UNUSED_CLIENT_TTL = timedelta(days=7)
_TOUCH_EVERY = timedelta(minutes=1)


class OAuthError(Exception):
    """OAuth 규약 오류 — 본문은 {"error", "error_description"} 모양으로 나간다(RFC 6749 §5.2)."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status

    def body(self) -> dict:
        return {"error": self.error, "error_description": self.description}


# --- 주소 ---------------------------------------------------------------------

def public_base() -> str:
    """브라우저·에이전트가 실제로 가는 주소(`http://gpax.lge.com`). 없으면 ""."""
    url = get_settings().platform_public_url.rstrip("/")
    return url if url.startswith(("http://", "https://")) else ""


def enabled() -> bool:
    settings = get_settings()
    return bool(settings.mcp_oauth_enabled and settings.oidc_provider_enabled and public_base())


def issuer() -> str:
    return f"{public_base()}/paas/oauth"


def metadata() -> dict:
    """발급자 메타데이터(RFC 8414). openid-configuration 경로로도 같은 문서를 준다 — 클라이언트가
    그 경로도 차례로 시도하고, 그때는 OIDC 필수 항목(jwks_uri 등)까지 있어야 받아 준다."""
    iss = issuer()
    return {
        "issuer": iss,
        "authorization_endpoint": f"{iss}/authorize",
        "token_endpoint": f"{iss}/token",
        "registration_endpoint": f"{iss}/register",
        "revocation_endpoint": f"{iss}/revoke",
        "jwks_uri": f"{public_base()}/paas/oauth2/jwks",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "service_documentation": f"{public_base()}/paas/api/v1/mcp/agent-setup.md",
    }


def resource_for(path_after_paas: str) -> str:
    """`/api/v1/mcp/hub` → `http://gpax.lge.com/paas/api/v1/mcp/hub`."""
    return f"{public_base()}/paas/{path_after_paas.lstrip('/')}".rstrip("/")


def protected_resource_metadata(path_after_paas: str) -> dict:
    """보호 자원 메타데이터(RFC 9728). resource는 클라이언트가 설정한 MCP 주소와 **글자 그대로**
    같아야 한다 — 클라이언트가 비교하고 다르면 연결을 멈춘다."""
    return {
        "resource": resource_for(path_after_paas),
        "authorization_servers": [issuer()],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "GPAX PaaS MCP",
        "resource_documentation": f"{public_base()}/paas/api/v1/mcp/agent-setup.md",
    }


def resource_metadata_url(request_path: str) -> str:
    """401 응답의 WWW-Authenticate에 실을 주소 — 요청 경로마다 그 자원의 메타데이터를 가리킨다."""
    rest = request_path[len("/paas"):] if request_path.startswith("/paas/") else request_path
    return f"{public_base()}/paas/.well-known/oauth-protected-resource{rest}"


def _check_resource(resource: str) -> str:
    """요청한 자원(RFC 8707)이 이 플랫폼의 API 아래인지. 비면 API 전체(/paas/api/v1)."""
    if not resource:
        return resource_for(API_PATH[len("/paas"):])
    parsed, base = urlparse(resource), urlparse(public_base())
    path = parsed.path.rstrip("/")
    if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc) or not (
        path == API_PATH or path.startswith(API_PATH + "/")
    ) or parsed.query or parsed.fragment:
        raise OAuthError("invalid_target", f"이 서버의 MCP 주소가 아닙니다: {resource}")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


# --- 클라이언트 등록 (RFC 7591) -------------------------------------------------

def _valid_redirect(uri: str) -> bool:
    """받는 돌아갈 주소: 이 PC(loopback http — RFC 8252), https, 앱 고유 스킴(cursor:// 등)."""
    if not uri or len(uri) > 512 or "#" in uri:
        return False
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if scheme == "http":
        return (parsed.hostname or "") in _LOOPBACK
    if scheme == "https":
        return bool(parsed.hostname)
    if not scheme or scheme in _FORBIDDEN_SCHEMES:
        return False
    return bool(parsed.netloc or parsed.path)  # 앱 고유 스킴(cursor://…, vscode://…)


def register(db: Session, body: dict) -> dict:
    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris or len(redirect_uris) > 10:
        raise OAuthError("invalid_redirect_uri", "redirect_uris를 1~10개 주세요.")
    bad = [u for u in redirect_uris if not isinstance(u, str) or not _valid_redirect(u)]
    if bad:
        raise OAuthError("invalid_redirect_uri",
                         f"받을 수 없는 돌아갈 주소입니다(이 PC의 http·https·앱 스킴만): {bad[0]}")
    grant_types = body.get("grant_types") or ["authorization_code", "refresh_token"]
    if "authorization_code" not in grant_types:
        raise OAuthError("invalid_client_metadata", "authorization_code 방식만 지원합니다.")
    name = str(body.get("client_name") or "").strip()[:128] or "MCP 클라이언트"

    # 등록은 로그인 없이 열려 있다(규약) — 승인으로 이어지지 않은 오래된 등록은 치운다.
    stale = utcnow() - _UNUSED_CLIENT_TTL
    used = select(McpOAuthGrant.client_id)
    db.execute(delete(McpOAuthClient).where(
        McpOAuthClient.created_at < stale, McpOAuthClient.client_id.not_in(used)))

    client_id = "mcp_" + secrets.token_urlsafe(18)
    row = McpOAuthClient(client_id=client_id, client_name=name,
                         redirect_uris=json.dumps(redirect_uris))
    db.add(row)
    db.commit()
    return {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "client_name": name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    }


def _client(db: Session, client_id: str) -> McpOAuthClient:
    row = db.execute(
        select(McpOAuthClient).where(McpOAuthClient.client_id == client_id)
    ).scalar_one_or_none()
    if row is None:
        raise OAuthError("invalid_client", f"등록되지 않은 client_id입니다: {client_id}", 401)
    return row


def _redirect_matches(registered: str, given: str) -> bool:
    """정확히 같아야 한다. 단 이 PC(loopback)의 포트는 실행마다 바뀌므로 포트만은 보지 않는다
    (RFC 8252 §7.3)."""
    if registered == given:
        return True
    a, b = urlparse(registered), urlparse(given)
    return (a.scheme == b.scheme == "http" and (a.hostname or "") in _LOOPBACK
            and a.hostname == b.hostname and a.path == b.path and a.query == b.query)


# --- 승인 (authorize) ----------------------------------------------------------

def check_authorize(db: Session, params: dict) -> dict:
    """authorize 요청 검증. 여기서 걸리는 client/redirect 오류는 **돌려보내지 않는다**(아직 믿을 수
    없는 주소라 open redirect가 된다) — 호출자가 화면에 오류로 보인다."""
    client = _client(db, params.get("client_id", ""))
    redirect_uri = params.get("redirect_uri", "")
    if not any(_redirect_matches(r, redirect_uri) for r in json.loads(client.redirect_uris)):
        raise OAuthError("invalid_request", f"등록되지 않은 돌아갈 주소입니다: {redirect_uri}")
    if params.get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "response_type=code만 지원합니다.")
    challenge = params.get("code_challenge", "")
    if params.get("code_challenge_method") != "S256" or not (43 <= len(challenge) <= 128):
        raise OAuthError("invalid_request", "PKCE(code_challenge_method=S256)가 필요합니다.")
    return {
        "client_id": client.client_id, "client_name": client.client_name,
        "redirect_uri": redirect_uri, "code_challenge": challenge,
        "state": params.get("state", ""), "resource": _check_resource(params.get("resource", "")),
    }


def _consent_secret() -> bytes:
    key = oidc_provider._get_signing_key().private_bytes(  # noqa: SLF001 — 같은 서명 키에서 파생
        encoding=serialization.Encoding.DER, format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    return hashlib.sha256(b"mcp-oauth-consent:" + key).digest()


def consent_ticket(req: dict, email: str, session_token: str) -> str:
    """승인 화면에 심는 표 — 이 사람(세션)이 이 요청을 본 화면에서 누른 것만 통과한다.

    승인 버튼은 폼 POST라, 표 없이 받으면 다른 사이트가 로그인된 사람의 브라우저로 같은 폼을
    보내 몰래 승인시킬 수 있다(CSRF). 표는 이 서버만 만들 수 있고 10분 뒤 버려진다."""
    exp = int(time.time() + CONSENT_TTL.total_seconds())
    msg = json.dumps([req, email, hash_key(session_token), exp], sort_keys=True,
                     ensure_ascii=False).encode()
    sig = hmac.new(_consent_secret(), msg, hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_ticket(ticket: str, req: dict, email: str, session_token: str) -> None:
    try:
        exp_s, sig = ticket.split(".", 1)
        exp = int(exp_s)
    except ValueError as e:
        raise OAuthError("access_denied", "승인 화면 정보가 올바르지 않습니다.") from e
    if exp < time.time():
        raise OAuthError("access_denied", "승인 화면이 오래되었습니다 — 다시 연결해 주세요.")
    msg = json.dumps([req, email, hash_key(session_token), exp], sort_keys=True,
                     ensure_ascii=False).encode()
    if not hmac.compare_digest(sig, hmac.new(_consent_secret(), msg, hashlib.sha256).hexdigest()):
        raise OAuthError("access_denied", "승인 화면 정보가 맞지 않습니다 — 다시 연결해 주세요.")


def issue_code(db: Session, req: dict, email: str) -> str:
    code = secrets.token_urlsafe(32)
    db.add(McpOAuthCode(
        code_hash=hash_key(code), client_id=req["client_id"], email=email,
        redirect_uri=req["redirect_uri"], code_challenge=req["code_challenge"],
        resource=req["resource"], expires_at=utcnow() + CODE_TTL,
    ))
    db.commit()
    return code


def redirect_with(redirect_uri: str, **params: str) -> str:
    sep = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{sep}{urlencode({k: v for k, v in params.items() if v})}"


# --- 토큰 ----------------------------------------------------------------------

def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _user(db: Session, email: str) -> UserAccount:
    user = db.execute(select(UserAccount).where(UserAccount.email == email)).scalar_one_or_none()
    if user is None or not user.is_approved:
        raise OAuthError("invalid_grant", "이 계정은 더 이상 쓸 수 없습니다.")
    return user


def _access_token(grant: McpOAuthGrant) -> str:
    now = int(time.time())
    payload = {
        "iss": issuer(), "sub": grant.email, "aud": grant.resource, "client_id": grant.client_id,
        "scope": SCOPE, "gid": grant.id, "iat": now,
        "exp": now + int(ACCESS_TOKEN_TTL.total_seconds()), "jti": secrets.token_urlsafe(12),
    }
    key_pem = oidc_provider._get_signing_key().private_bytes(  # noqa: SLF001
        encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption())
    return jwt.encode(payload, key_pem, algorithm="RS256",
                      headers={"kid": oidc_provider.key_id(), "typ": ACCESS_TOKEN_TYP})


def _token_response(grant: McpOAuthGrant, refresh: str) -> dict:
    return {
        "access_token": _access_token(grant), "token_type": "Bearer",
        "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()), "refresh_token": refresh,
        "scope": SCOPE,
    }


def _pkce_ok(verifier: str, challenge: str) -> bool:
    if not (43 <= len(verifier) <= 128):
        return False
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return hmac.compare_digest(digest.decode(), challenge)


def exchange_code(db: Session, form: dict) -> dict:
    code = form.get("code", "")
    row = db.execute(
        select(McpOAuthCode).where(McpOAuthCode.code_hash == hash_key(code))
    ).scalar_one_or_none()
    if row is None:
        raise OAuthError("invalid_grant", "code가 없거나 이미 쓰였습니다.")
    db.delete(row)  # 조회 즉시 지운다 — 맞든 틀리든 두 번은 못 쓴다
    db.commit()
    if _aware(row.expires_at) <= utcnow():
        raise OAuthError("invalid_grant", "code가 만료되었습니다(60초).")
    if row.client_id != form.get("client_id") or row.redirect_uri != form.get("redirect_uri"):
        raise OAuthError("invalid_grant", "client_id 또는 redirect_uri가 승인 때와 다릅니다.")
    if not _pkce_ok(form.get("code_verifier", ""), row.code_challenge):
        raise OAuthError("invalid_grant", "PKCE code_verifier가 맞지 않습니다.")
    resource = row.resource
    if form.get("resource") and _check_resource(form["resource"]) != resource:
        raise OAuthError("invalid_target", "resource가 승인 때와 다릅니다.")
    _user(db, row.email)
    client = _client(db, row.client_id)

    # 같은 앱·같은 사람·같은 주소로 다시 승인하면 옛 승인을 대신한다 — 목록에 쌓이지 않게.
    db.execute(delete(McpOAuthGrant).where(
        McpOAuthGrant.client_id == row.client_id, McpOAuthGrant.email == row.email,
        McpOAuthGrant.resource == resource))
    refresh = "mcpr_" + secrets.token_urlsafe(32)
    grant = McpOAuthGrant(
        client_id=row.client_id, client_name=client.client_name, email=row.email,
        resource=resource, refresh_hash=hash_key(refresh), expires_at=utcnow() + GRANT_TTL,
        last_used_at=utcnow(),
    )
    db.add(grant)
    db.commit()
    return _token_response(grant, refresh)


def refresh(db: Session, form: dict) -> dict:
    grant = db.execute(select(McpOAuthGrant).where(
        McpOAuthGrant.refresh_hash == hash_key(form.get("refresh_token", ""))
    )).scalar_one_or_none()
    if grant is None or grant.client_id != form.get("client_id", grant.client_id):
        raise OAuthError("invalid_grant", "갱신 토큰이 없거나 끊긴 연결입니다 — 다시 로그인하세요.")
    if _aware(grant.expires_at) <= utcnow():
        db.delete(grant)
        db.commit()
        raise OAuthError("invalid_grant", "승인 기간(90일)이 지났습니다 — 다시 로그인하세요.")
    _user(db, grant.email)
    new = "mcpr_" + secrets.token_urlsafe(32)  # 쓸 때마다 바꾼다 — 새어 나간 옛 값은 바로 무효
    grant.refresh_hash = hash_key(new)
    grant.last_used_at = utcnow()
    db.commit()
    return _token_response(grant, new)


def revoke(db: Session, token: str) -> None:
    """RFC 7009 — 갱신 토큰이든 액세스 토큰이든 그 연결을 끊는다. 모르는 값이어도 200."""
    grant = db.execute(select(McpOAuthGrant).where(
        McpOAuthGrant.refresh_hash == hash_key(token))).scalar_one_or_none()
    if grant is None and token.count(".") == 2:
        try:
            gid = jwt.decode(token, options={"verify_signature": False}).get("gid")
        except jwt.PyJWTError:
            gid = None
        grant = db.get(McpOAuthGrant, gid) if isinstance(gid, int) else None
        if grant is not None:  # 서명까지 맞아야 끊는다 — 남의 연결을 gid만으로 못 끊게
            try:
                _decode(token)
            except OAuthError:
                grant = None
    if grant is not None:
        db.delete(grant)
        db.commit()


# --- 검증 (MCP 요청마다) ---------------------------------------------------------

def is_access_token(token: str) -> bool:
    """우리 MCP 액세스 토큰 모양인가(헤더만 본다 — 검증은 verify가 한다)."""
    if token.count(".") != 2:
        return False
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return False
    return header.get("typ") == ACCESS_TOKEN_TYP


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, oidc_provider.public_key_pem(), algorithms=["RS256"],
                          issuer=issuer(), options={"verify_aud": False})
    except jwt.PyJWTError as e:
        raise OAuthError("invalid_token", f"토큰이 유효하지 않습니다: {e}", 401) from e


def verify(db: Session, token: str, request_path: str) -> tuple[str, bool]:
    """MCP 요청의 액세스 토큰 → (이메일, 관리자 여부).

    대상(aud)은 승인한 MCP 주소다. 요청 경로가 그 주소(또는 그 아래)일 때만 받는다 — 허브에
    붙으라고 준 토큰으로 다른 서버를 못 연다(RFC 8707). 경로로만 비교한다: 앱은 IIS 뒤에서
    자기 주소를 localhost로 보기 때문이다."""
    claims = _decode(token)
    aud = urlparse(str(claims.get("aud", ""))).path.rstrip("/")
    path = request_path.rstrip("/")
    if not aud or not (path == aud or path.startswith(aud + "/")):
        raise OAuthError("invalid_token", f"이 토큰은 {aud or '(없음)'}용입니다.", 401)
    grant = db.get(McpOAuthGrant, claims.get("gid")) if isinstance(claims.get("gid"), int) else None
    if grant is None or grant.email != claims.get("sub"):
        raise OAuthError("invalid_token", "끊긴 연결입니다 — 다시 로그인하세요.", 401)
    try:
        user = _user(db, grant.email)
    except OAuthError as e:
        raise OAuthError("invalid_token", e.description, 401) from e
    if grant.last_used_at is None or _aware(grant.last_used_at) <= utcnow() - _TOUCH_EVERY:
        grant.last_used_at = utcnow()  # 쓰이고 있는 연결인지 — 끊을 근거
        db.commit()
    return grant.email, user.is_admin


# --- 내 승인 목록 ----------------------------------------------------------------

def my_grants(db: Session, email: str) -> list[dict]:
    rows = db.execute(select(McpOAuthGrant).where(McpOAuthGrant.email == email)
                      .order_by(McpOAuthGrant.created_at.desc())).scalars()
    now = utcnow()
    return [{
        "id": g.id, "client_name": g.client_name, "resource": g.resource,
        "created_at": _aware(g.created_at).isoformat(),
        "expires_at": _aware(g.expires_at).isoformat(),
        "last_used_at": _aware(g.last_used_at).isoformat() if g.last_used_at else None,
        "is_expired": _aware(g.expires_at) <= now,
    } for g in rows]


def drop_grant(db: Session, email: str, grant_id: int) -> bool:
    grant = db.get(McpOAuthGrant, grant_id)
    if grant is None or grant.email != email:
        return False
    db.delete(grant)
    db.commit()
    return True
