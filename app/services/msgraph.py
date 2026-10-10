"""아웃룩 메일 — Device Code로 로그인하고 받은편지함을 읽는다(Microsoft 365, Graph).

AWS SSO 로그인과 같은 모양이다: 서버가 로그인을 시작해 코드를 내주고, 사람은 **자기 브라우저**
에서 주소를 열어 코드를 넣고 승인한다. 팝업·리디렉션이 없어서 http 콘솔이나 SPA 리디렉션 URI
등록 없이도 된다(앱 등록에는 "공용 클라이언트 흐름 허용"만 켜면 된다).

**토큰은 저장하지 않는다.** 승인이 끝났는지 묻는 요청(finish) 안에서 받아 바로 메일을 읽고
버린다 — 메모리 딕셔너리에도 남지 않는다. offline_access를 요청하지 않으므로 refresh
token도 없다. 서버가 들고 있는 것은 승인 대기 중인 device_code뿐이고, 그것만으로는 메일을
읽을 수 없다(사람이 승인한 뒤에야 토큰으로 바뀐다). 승인한 계정이 곧 메일의 주인이다:
다른 사람이 승인하면 그 사람의 메일이 이 사람의 맥락에 들어오지만, 그러려면 그 사람의
자격증명이 있어야 한다.
"""
import threading
import time

import httpx

from ..config import get_settings

SCOPE = "Mail.Read User.Read"
GRAPH = "https://graph.microsoft.com/v1.0"
TIMEOUT = 20.0

# 승인 대기 중인 로그인 — 사용자(이메일)당 하나. 서버 프로세스 하나에서만 맞는다(AWS 로그인과 같다).
_pending: dict[str, dict] = {}
_lock = threading.Lock()


class MailLoginError(Exception):
    pass


def _authority() -> str:
    settings = get_settings()
    return f"https://login.microsoftonline.com/{settings.ms_graph_tenant}/oauth2/v2.0"


def _error(body: dict, fallback: str) -> str:
    detail = str(body.get("error_description") or body.get("error") or fallback)
    # 앱 등록에서 공용 클라이언트 흐름이 꺼져 있으면 이 코드가 온다 — 고칠 곳을 알려 준다.
    if "AADSTS7000218" in detail or "AADSTS70002" in detail:
        detail += " (Entra ID 앱 등록 > 인증 > '공용 클라이언트 흐름 허용'을 켜세요)"
    return detail


def start(email: str) -> dict:
    """로그인을 시작하고 사람이 할 일(주소·코드)을 돌려준다."""
    settings = get_settings()
    if not settings.ms_graph_client_id:
        raise MailLoginError("메일 연동이 설정되지 않았습니다 — 관리자가 PAAS_MS_GRAPH_CLIENT_ID를 지정해야 합니다.")
    try:
        res = httpx.post(f"{_authority()}/devicecode", timeout=TIMEOUT,
                         data={"client_id": settings.ms_graph_client_id, "scope": SCOPE})
        body = res.json()
    except (httpx.HTTPError, ValueError) as e:
        raise MailLoginError(f"Microsoft에 연결하지 못했습니다: {e}")
    if res.status_code != 200:
        raise MailLoginError(f"메일 로그인을 시작하지 못했습니다: {_error(body, f'HTTP {res.status_code}')}")
    with _lock:
        _pending[email] = {"device_code": body["device_code"],
                           "expires": time.monotonic() + int(body.get("expires_in", 900))}
    return {"verification_url": body["verification_uri"], "user_code": body["user_code"],
            "expires_in": int(body.get("expires_in", 900)), "interval": int(body.get("interval", 5))}


def finish(email: str, count: int) -> dict | None:
    """승인이 끝났으면 받은편지함 최근 `count`통을 읽어 돌려주고, 아직이면 None."""
    settings = get_settings()
    with _lock:
        pending = _pending.get(email)
    if pending is None or pending["expires"] < time.monotonic():
        with _lock:
            _pending.pop(email, None)
        raise MailLoginError("진행 중인 메일 로그인이 없거나 시간이 지났습니다 — 다시 시작하세요.")
    try:
        res = httpx.post(f"{_authority()}/token", timeout=TIMEOUT, data={
            "client_id": settings.ms_graph_client_id,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": pending["device_code"]})
        body = res.json()
    except (httpx.HTTPError, ValueError) as e:
        raise MailLoginError(f"Microsoft에 연결하지 못했습니다: {e}")
    if res.status_code != 200:
        if body.get("error") in ("authorization_pending", "slow_down"):
            return None
        with _lock:
            _pending.pop(email, None)
        raise MailLoginError(f"메일 로그인에 실패했습니다: {_error(body, f'HTTP {res.status_code}')}")
    with _lock:
        _pending.pop(email, None)
    return _read_inbox(body["access_token"], count)


def _read_inbox(token: str, count: int) -> dict:
    # 본문을 HTML이 아니라 텍스트로 받는다 — 색인·온톨로지는 텍스트를 본다.
    headers = {"Authorization": f"Bearer {token}", "Prefer": 'outlook.body-content-type="text"'}

    def get(path: str, params: dict | None = None) -> dict:
        try:
            res = httpx.get(f"{GRAPH}{path}", headers=headers, params=params, timeout=TIMEOUT)
        except httpx.HTTPError as e:
            raise MailLoginError(f"Microsoft Graph에 연결하지 못했습니다: {e}")
        if res.status_code != 200:
            raise MailLoginError(f"Microsoft Graph가 HTTP {res.status_code}로 답했습니다: {res.text[:300]}")
        return res.json()

    me = get("/me", {"$select": "mail,userPrincipalName"})
    inbox = get("/me/mailFolders/inbox/messages", {
        "$top": count, "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,toRecipients,receivedDateTime,body,webLink"})
    return {"account": me.get("mail") or me.get("userPrincipalName") or "",
            "messages": inbox.get("value", [])}
