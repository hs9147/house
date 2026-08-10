"""paas 자체 OIDC Provider 엔드포인트 — services/oidc_provider.py의 얇은 HTTP 껍데기.

PAAS_OIDC_PROVIDER_ENABLED=true일 때만 main.py가 이 라우터를 붙인다. 버전 prefix를
받지 않는다(webhooks·health와 같은 이유 — 외부 서비스가 한 번 등록해 두는 안정된
프로토콜 경로라 API 버전이 올라가도 안 깨져야 한다).
"""
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Form, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import oidc_provider

router = APIRouter(tags=["oidc-provider"])


@router.get("/.well-known/openid-configuration")
def discovery():
    return oidc_provider.discovery_document()


@router.get("/oauth2/jwks")
def jwks():
    return oidc_provider.jwks()


@router.get("/oauth2/authorize")
def authorize(
    request: Request,
    client_id: str,
    redirect_uri: str,
    response_type: str = "code",
    scope: str = "openid",
    state: str = "",
    nonce: str = "",
    db: Session = Depends(get_db),
    paas_session: str = Cookie(default=""),
):
    """미로그인이면 콘솔 로그인 화면으로, 로그인돼 있으면 code를 발급해 클라이언트로
    돌려보낸다. client_id/redirect_uri가 등록돼 있지 않으면 어디로도 리다이렉트하지
    않는다(신뢰 안 된 URL로 보내는 open redirect가 되므로 여기서 바로 400)."""
    try:
        oidc_provider.validate_authorize_request(client_id, redirect_uri, response_type)
    except oidc_provider.OidcProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))

    email = oidc_provider.email_for_session(db, paas_session)
    if email is None:
        next_url = str(request.url.path)
        if request.url.query:
            next_url += f"?{request.url.query}"
        return RedirectResponse(oidc_provider.login_redirect_url(next_url))

    code = oidc_provider.issue_auth_code(db, client_id, email, redirect_uri, nonce)
    sep = "&" if "?" in redirect_uri else "?"
    redirect_to = f"{redirect_uri}{sep}code={quote(code)}"
    if state:
        redirect_to += f"&state={quote(state)}"
    return RedirectResponse(redirect_to)


@router.post("/oauth2/token")
def token(
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
    client_id: str = Form(default=""),
    client_secret: str = Form(default=""),
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")

    cid, secret = client_id, client_secret
    if not cid and authorization.lower().startswith("basic "):
        import base64
        try:
            decoded = base64.b64decode(authorization[6:].strip()).decode()
            cid, secret = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(status_code=401, detail="invalid client authentication") from e

    try:
        oidc_provider.exchange_client_credentials(cid, secret)
        email, nonce = oidc_provider.consume_auth_code(db, code, cid, redirect_uri)
    except oidc_provider.OidcProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))

    id_token = oidc_provider.issue_id_token(email, cid, nonce)
    return {
        "access_token": id_token,
        "id_token": id_token,
        "token_type": "Bearer",
        "expires_in": int(oidc_provider.ID_TOKEN_TTL.total_seconds()),
    }


@router.get("/oauth2/userinfo")
def userinfo(authorization: str = Header(default="")):
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="bearer token required")
    try:
        claims = oidc_provider.decode_id_token(authorization[7:].strip())
    except oidc_provider.OidcProviderError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {
        "sub": claims.get("sub"),
        "email": claims.get("email"),
        "preferred_username": claims.get("preferred_username"),
    }
