"""GPAX 계정 로그인으로 MCP 연결 — services/mcp_oauth.py의 HTTP 껍데기.

경로 세 갈래:
  - router(/paas 아래): 발급자(/paas/oauth/*)와 보호 자원 메타데이터
  - root_router(접두사 없음): `/.well-known/…/paas/…` — MCP 클라이언트는 발급자 메타데이터를
    **도메인 뿌리**에서 먼저 찾는다. 뿌리는 이 앱이 아니라(gpax.lge.com은 GPAX 포털) IIS가
    그 경로만 이 앱으로 넘겨 줘야 닿는다(docs/mcp-oauth.md).
  - grants_router(/paas/api/v1 아래): 「승인된 앱」 목록·끊기 — 콘솔이 부른다.
"""
import html
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey
from ..security import require_api_key
from ..services import mcp_oauth, oidc_provider
from ..services.mcp_oauth import OAuthError

router = APIRouter(tags=["mcp-oauth"])
root_router = APIRouter(tags=["mcp-oauth"])
grants_router = APIRouter(tags=["mcp"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _require_enabled() -> None:
    if not mcp_oauth.enabled():
        raise HTTPException(status_code=404, detail=(
            "GPAX 로그인으로 MCP 연결이 꺼져 있습니다 — PAAS_OIDC_PROVIDER_ENABLED와 "
            "PAAS_PLATFORM_PUBLIC_URL이 필요합니다. 개인 MCP 토큰은 계속 쓸 수 있습니다."))


def _oauth_error(e: OAuthError) -> JSONResponse:
    return JSONResponse(e.body(), status_code=e.status, headers=_NO_STORE)


# --- 메타데이터 -------------------------------------------------------------------

def _as_metadata():
    _require_enabled()
    return mcp_oauth.metadata()


def _prm(path: str):
    _require_enabled()
    return mcp_oauth.protected_resource_metadata(path)


router.add_api_route("/oauth/.well-known/oauth-authorization-server", _as_metadata, methods=["GET"])
router.add_api_route("/oauth/.well-known/openid-configuration", _as_metadata, methods=["GET"])
router.add_api_route("/.well-known/oauth-authorization-server/oauth", _as_metadata, methods=["GET"])
# IIS가 뿌리의 `/.well-known/…/paas/…`를 넘겨 줄 때 받는 곳(RFC 8414 §3.1의 경로 삽입형).
root_router.add_api_route("/.well-known/oauth-authorization-server/paas/oauth", _as_metadata,
                          methods=["GET"])
root_router.add_api_route("/.well-known/openid-configuration/paas/oauth", _as_metadata,
                          methods=["GET"])


@router.get("/.well-known/oauth-protected-resource")
def prm_root():
    return _prm(mcp_oauth.API_PATH[len("/paas"):])


@router.get("/.well-known/oauth-protected-resource/{path:path}")
def prm(path: str):
    return _prm(path)


@root_router.get("/.well-known/oauth-protected-resource/paas/{path:path}")
def prm_from_root(path: str):
    return _prm(path)


# --- 등록 ----------------------------------------------------------------------

@router.post("/oauth/register")
async def register(request: Request, db: Session = Depends(get_db)):
    _require_enabled()
    try:
        body = await request.json()
    except ValueError:
        return _oauth_error(OAuthError("invalid_client_metadata", "JSON 본문이 필요합니다."))
    if not isinstance(body, dict):
        return _oauth_error(OAuthError("invalid_client_metadata", "JSON 객체가 필요합니다."))
    try:
        out = mcp_oauth.register(db, body)
    except OAuthError as e:
        return _oauth_error(e)
    print(f"[paas] MCP OAuth 클라이언트 등록: {out['client_name']} ({out['client_id']}) "
          f"redirect={out['redirect_uris']}")
    return JSONResponse(out, status_code=201, headers=_NO_STORE)


# --- 로그인·승인 -------------------------------------------------------------------

_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
body{{margin:0;background:#12131f;color:#e4e6f0;font:15px/1.6 'Malgun Gothic','Apple SD Gothic Neo',sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center}}
.card{{background:#1a1b2a;border:1px solid #2c2e44;border-radius:12px;padding:28px 32px;max-width:520px;width:100%}}
h1{{font-size:19px;margin:0 0 6px}} .sub{{color:#9aa0b8;margin:0 0 20px}}
dl{{margin:0 0 20px;display:grid;grid-template-columns:96px 1fr;gap:10px 12px}}
dt{{color:#9aa0b8}} dd{{margin:0}} code{{font-family:Consolas,'Cascadia Mono',monospace;font-size:13px}}
.warn{{background:#3a2a10;border:1px solid #6b4d12;color:#f0c674;border-radius:8px;padding:10px 12px;margin:0 0 18px}}
.row{{display:flex;gap:10px;justify-content:flex-end}}
button{{font:inherit;border-radius:8px;padding:9px 18px;cursor:pointer;border:1px solid #3a3d5c}}
.ok{{background:#3987e5;border-color:#3987e5;color:#fff}} .no{{background:transparent;color:#e4e6f0}}
.muted{{color:#7d839c;font-size:13px;margin:16px 0 0}}
</style></head><body><div class="card">{body}</div></body></html>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    # 남의 사이트가 이 화면을 투명하게 겹쳐 승인을 누르게 하지 못하게(클릭재킹).
    headers = {**_NO_STORE, "X-Frame-Options": "DENY",
               "Content-Security-Policy": "frame-ancestors 'none'"}
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=body), status_code=status,
                        headers=headers)


def _error_page(message: str, status: int = 400) -> HTMLResponse:
    return _page("MCP 연결 실패", (
        "<h1>MCP 연결을 시작할 수 없습니다</h1>"
        f"<p class='sub'>{html.escape(message)}</p>"
        "<p class='muted'>에이전트(Claude Code 등)에서 연결을 다시 시작해 보세요. 계속되면 "
        "콘솔의 「MCP 연결설정」에서 개인 토큰으로 연결할 수 있습니다.</p>"), status)


def _where_back(redirect_uri: str) -> tuple[str, bool]:
    """돌아갈 곳을 사람 말로 — (설명, 이 PC 밖인가)."""
    parsed = urlparse(redirect_uri)
    if parsed.scheme == "http" and (parsed.hostname or "") in mcp_oauth._LOOPBACK:  # noqa: SLF001
        return f"이 PC에서 실행 중인 프로그램 (localhost:{parsed.port or 80})", False
    if parsed.scheme == "https":
        return parsed.netloc, True
    return f"{parsed.scheme}:// 앱", False


def _target_label(resource: str) -> str:
    path = urlparse(resource).path
    if path.endswith("/mcp/hub"):
        return f"통합 MCP 서버 ({path})"
    if path.rstrip("/") == mcp_oauth.API_PATH:
        return f"이 플랫폼의 MCP·게이트웨이 전체 ({path})"
    return path


_HIDDEN = ("client_id", "redirect_uri", "code_challenge", "state", "resource")


def _consent(req: dict, email: str, ticket: str) -> HTMLResponse:
    back, outside = _where_back(req["redirect_uri"])
    hidden = "".join(
        f"<input type='hidden' name='{k}' value='{html.escape(req[k], quote=True)}'>"
        for k in _HIDDEN)
    warn = ("<p class='warn'>승인하면 이 PC 밖의 주소로 돌아갑니다. 직접 시작한 연결이 아니면 "
            "거부하세요.</p>") if outside else ""
    body = (
        "<h1>MCP 연결 승인</h1>"
        f"<p class='sub'><b>{html.escape(req['client_name'])}</b>이(가) GPAX 계정으로 "
        "MCP에 연결하려 합니다.</p>"
        "<dl>"
        f"<dt>계정</dt><dd>{html.escape(email)}</dd>"
        f"<dt>연결할 곳</dt><dd><code>{html.escape(_target_label(req['resource']))}</code></dd>"
        "<dt>할 수 있는 일</dt><dd>내 계정 권한 안에서 MCP 도구(문서·온톨로지·과제…)를 읽고 "
        "실행합니다. 콘솔·배포·계정 관리는 할 수 없습니다.</dd>"
        f"<dt>돌아갈 곳</dt><dd>{html.escape(back)}</dd>"
        "<dt>기간</dt><dd>90일(1시간마다 자동 갱신). 콘솔 「MCP 연결설정 → 승인된 앱」에서 "
        "언제든 끊을 수 있습니다.</dd>"
        "</dl>"
        f"{warn}"
        "<form method='post' action=''>"
        f"{hidden}<input type='hidden' name='ticket' value='{html.escape(ticket, quote=True)}'>"
        "<div class='row'>"
        "<button class='no' name='decision' value='deny'>거부</button>"
        "<button class='ok' name='decision' value='approve' autofocus>승인</button>"
        "</div></form>"
        f"<p class='muted'>{html.escape(email)} 계정이 아니면 콘솔에서 로그아웃한 뒤 연결을 "
        "다시 시작하세요.</p>"
    )
    return _page("MCP 연결 승인", body)


@router.get("/oauth/authorize")
def authorize(request: Request, db: Session = Depends(get_db),
              paas_session: str = Cookie(default="")):
    """Gitea SSO와 같은 길: 미로그인이면 콘솔 로그인으로 보냈다가 이 주소로 돌아온다. 다른 점은
    로그인돼 있어도 code를 바로 주지 않고 승인 화면을 띄운다는 것 — 누구나 등록하는 공개
    클라이언트라, 사람이 「누가·어디로」를 보고 눌러야 한다."""
    _require_enabled()
    try:
        req = mcp_oauth.check_authorize(db, dict(request.query_params))
    except OAuthError as e:
        return _error_page(e.description)
    email = oidc_provider.email_for_session(db, paas_session)
    if email is None:
        next_url = f"{request.url.path}?{request.url.query}"
        return RedirectResponse(oidc_provider.login_redirect_url(next_url))
    return _consent(req, email, mcp_oauth.consent_ticket(req, email, paas_session))


@router.post("/oauth/authorize")
async def decide(request: Request, db: Session = Depends(get_db),
                 paas_session: str = Cookie(default="")):
    _require_enabled()
    form = dict(await request.form())
    params = {k: str(form.get(k, "")) for k in _HIDDEN}
    params.update(response_type="code", code_challenge_method="S256")
    try:
        req = mcp_oauth.check_authorize(db, params)
    except OAuthError as e:
        return _error_page(e.description)
    email = oidc_provider.email_for_session(db, paas_session)
    if email is None:
        return _error_page("로그인이 풀렸습니다 — 에이전트에서 연결을 다시 시작하세요.", 401)
    try:
        mcp_oauth.check_ticket(str(form.get("ticket", "")), req, email, paas_session)
    except OAuthError as e:
        return _error_page(e.description, 403)

    if form.get("decision") != "approve":
        print(f"[paas] MCP OAuth 거부: {req['client_name']} user={email}")
        return RedirectResponse(mcp_oauth.redirect_with(
            req["redirect_uri"], error="access_denied", state=req["state"]), status_code=303)
    code = mcp_oauth.issue_code(db, req, email)
    audit.record(db, email, "mcp.oauth.approve", req["client_name"],
                 {"client_id": req["client_id"], "resource": req["resource"],
                  "redirect_uri": req["redirect_uri"]})
    print(f"[paas] MCP OAuth 승인 → code 발급: {req['client_name']} user={email} "
          f"resource={req['resource']}")
    return RedirectResponse(mcp_oauth.redirect_with(
        req["redirect_uri"], code=code, state=req["state"]), status_code=303)


# --- 토큰 ----------------------------------------------------------------------

@router.post("/oauth/token")
async def token(request: Request, db: Session = Depends(get_db)):
    _require_enabled()
    form = {k: str(v) for k, v in (await request.form()).items()}
    grant_type = form.get("grant_type", "")
    try:
        if grant_type == "authorization_code":
            out = mcp_oauth.exchange_code(db, form)
        elif grant_type == "refresh_token":
            out = mcp_oauth.refresh(db, form)
        else:
            raise OAuthError("unsupported_grant_type", f"지원하지 않는 grant_type: {grant_type}")
    except OAuthError as e:
        print(f"[paas] MCP OAuth token 실패({grant_type}): client={form.get('client_id')} — "
              f"{e.description}")
        return _oauth_error(e)
    return JSONResponse(out, headers=_NO_STORE)


@router.post("/oauth/revoke")
async def revoke(request: Request, db: Session = Depends(get_db)):
    _require_enabled()
    form = await request.form()
    mcp_oauth.revoke(db, str(form.get("token", "")))
    return JSONResponse({}, headers=_NO_STORE)


# --- 로그인 도우미 -----------------------------------------------------------------

_HELPER = Path(__file__).resolve().parent.parent / "services" / "mcp_login_helper.mjs"


@grants_router.get("/mcp/paas-mcp.mjs")
def login_helper():
    """GPAX MCP 로그인 도우미(Node) — 로그인 없이 받는다(비밀이 없다). 서버 주소만 채워 내려 준다.

    MCP 클라이언트가 스스로 OAuth를 하려면 토큰 주소가 https여야 한다(Claude Code는 http면 거부한다). 이 플랫폼이
    아직 http라, 로그인·갱신은 이 도우미가 하고 Claude Code에는 headersHelper로 헤더만 건넨다."""
    _require_enabled()
    script = _HELPER.read_text(encoding="utf-8").replace("__PAAS_BASE__", mcp_oauth.public_base())
    return Response(script, media_type="text/javascript; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="paas-mcp.mjs"', "Cache-Control": "no-cache"})


# --- 승인된 앱 (콘솔) ---------------------------------------------------------------

@grants_router.get("/mcp/oauth/grants")
def my_grants(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    """내가 GPAX 로그인으로 승인한 MCP 연결. enabled가 false면 이 서버는 그 길이 꺼져 있다."""
    return {"enabled": mcp_oauth.enabled(), "grants": mcp_oauth.my_grants(db, key.name)}


@grants_router.delete("/mcp/oauth/grants/{grant_id}", status_code=204)
def drop_grant(grant_id: int, db: Session = Depends(get_db),
               key: ApiKey = Depends(require_api_key)):
    if not mcp_oauth.drop_grant(db, key.name, grant_id):
        raise HTTPException(status_code=404, detail="그런 연결이 없습니다.")
    audit.record(db, key.name, "mcp.oauth.revoke", str(grant_id), {})
