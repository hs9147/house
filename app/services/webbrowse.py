"""웹 페이지 읽기 — 서버 브라우저(Playwright)로 그린 화면을 마크다운으로(MCP /mcp/browser, 「paas-browser」).

**읽기만 한다.** 주소를 열어 다 그려지면 제목·본문(마크다운)·링크를 돌려준다. 누르기·입력·로그인은 하지 않는다 —
아무 사이트에서나 누르고 채우는 도구(Playwright MCP)는 서버가 사내망 안에 있어 보안 검증을 통과하기 어렵고,
Node 서버를 따로 띄워야 한다. 같은 Playwright를 쓰되 할 수 있는 일을 좁혔다.

**허용한 호스트만 연다**(PAAS_BROWSER_ALLOWED_HOSTS — 그 호스트와 하위 도메인). 서버는 사내망 안에 있어, 아무
주소나 열게 하면 밖에서 닿지 않는 사내 시스템(관리 화면·메타데이터 주소)을 LLM이 대신 열어 주는 통로가 된다.
화면이 부르는 하위 자원(스크립트·그림·리다이렉트·틀)도 허용한 호스트로만 간다 — 허용하지 않은 호스트의 자원은
막는다(그 자원 없이 그려진 대로 읽는다. 필요한 CDN은 허용 목록에 더한다). 쿠키·헤더는 싣지 않는다(로그인한
화면은 못 본다). 보안·비용 검증을 마치고 PAAS_BROWSER_APPROVED=true여야 켜진다(docs/web-browser-review.md).
"""
import threading
import time
from urllib.parse import urlsplit

from ..config import get_settings
from . import infosource

MAX_CHARS = 20_000
MAX_LINKS = 60
TIMEOUT = 20            # 초 — 다 그려지기(load)까지
SETTLE = 5              # 초 — 그 뒤 요청이 잦아들기(networkidle)까지 더 기다리는 상한
_slots = threading.Semaphore(2)   # 브라우저는 무겁다 — 동시에 둘까지
_lock = threading.Lock()
_used: dict[str, list[float]] = {}


class BrowseError(Exception):
    pass


def allowed_hosts() -> list[str]:
    return [h.strip().lower().lstrip(".") for h in get_settings().browser_allowed_hosts.split(",") if h.strip()]


def host_allowed(host: str, hosts: list[str] | None = None) -> bool:
    host = (host or "").lower().rstrip(".")
    return bool(host) and any(host == h or host.endswith("." + h) for h in (allowed_hosts() if hosts is None else hosts))


def state() -> str:
    """on(쓸 수 있다) · no_hosts(열 호스트를 정하지 않았다) · pending(검증 전) · no_browser(Playwright 없음)."""
    if not infosource.browser_available():
        return "no_browser"
    if not allowed_hosts():
        return "no_hosts"
    return "on" if get_settings().browser_approved else "pending"


def configured() -> bool:
    return state() == "on"


def _check(actor: str, url: str) -> str:
    url = str(url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BrowseError("http·https 주소만 엽니다.")
    st = state()
    if st != "on":
        raise BrowseError({"no_browser": "서버에 브라우저(Playwright)가 없습니다.",
                           "no_hosts": "열 수 있는 호스트가 정해지지 않았습니다(PAAS_BROWSER_ALLOWED_HOSTS).",
                           "pending": "웹 페이지 읽기는 보안·비용 검증 전이라 아직 쓸 수 없습니다."}[st])
    if not host_allowed(parts.hostname):
        raise BrowseError(f"허용하지 않은 호스트입니다: {parts.hostname} — 열 수 있는 곳: {', '.join(allowed_hosts())}")
    limit = get_settings().browser_max_per_hour
    with _lock:
        now = time.time()
        recent = [t for t in _used.get(actor, []) if now - t < 3600]
        if len(recent) >= limit:
            raise BrowseError(f"한 시간에 {limit}번까지 열 수 있습니다 — 잠시 뒤에 다시 하세요.")
        recent.append(now)
        _used[actor] = recent
    return url


def read_page(actor: str, url: str) -> dict:
    """주소 하나를 그려 읽는다 — {url(최종), status, title, markdown, truncated, links, blocked}."""
    url = _check(actor, url)
    from playwright.sync_api import Error as PlaywrightError  # noqa: PLC0415
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    hosts, blocked = allowed_hosts(), set()

    def gate(route, request):
        host = urlsplit(request.url).hostname or ""
        if host_allowed(host, hosts):
            route.continue_()
        else:
            blocked.add(host or request.url.split(":", 1)[0])
            route.abort()

    with _slots, sync_playwright() as pw:
        try:
            browser, _ = infosource._launch(pw)
        except infosource.SourceError as e:
            raise BrowseError(str(e))
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900},
                                          user_agent=infosource.USER_AGENT)
            context.route("**/*", gate)
            page = context.new_page()
            try:
                response = page.goto(url, wait_until="load", timeout=TIMEOUT * 1000)
            except PlaywrightError as e:
                raise BrowseError(f"열지 못했습니다: {str(e).splitlines()[0][:200]}")
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE * 1000)
            except PlaywrightError:
                pass   # 계속 요청하는 화면(폴링) — 그려진 데까지 읽는다
            final, html, title = page.url, page.content(), page.title()
        finally:
            browser.close()
    markdown = infosource._markdown(html)
    links = [{"url": link["url"], "text": link["text"]} for link in infosource._read_page(final, html)["links"]
             if host_allowed(urlsplit(link["url"]).hostname or "", hosts)][:MAX_LINKS]
    return {"url": final, "status": response.status if response else None, "title": title,
            "markdown": markdown[:MAX_CHARS], "truncated": len(markdown) > MAX_CHARS,
            "links": links, "blocked": sorted(blocked)[:20]}
