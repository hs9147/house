"""서버 봇 — 북마크의 수집 엔진(console/src/lib/scanAgent.js)을 서버 브라우저에서 그대로 돌린다.

북마크는 사람이 로그인한 탭에서 돈다. 서버가 같은 일을 하려고 메뉴 길 밟기·틀(iframe) 찾기·조회 응답에서 목록
전체 받기·엑셀 버튼·상세의 첨부를 파이썬으로 다시 짜면 두 벌이 갈라진다 — 그래서 **같은 코드를 서버 브라우저에
넣는다.** 북마크는 콘솔(받는 창)과 postMessage로만 말하므로, 받는 창을 서버가 흉내 낸다: 사이트 탭에서 엔진이 받는
창을 팝업으로 열면 그 주소(가짜 출처 RUNNER)를 가로채 작은 페이지로 답하고, 그 페이지가 인사에 표(nonce)·수집
조건·할 일(replay)을 보내고 결과를 파이썬으로 넘긴다. 엔진이 하는 일과 하지 않는 일(저장·삭제 버튼, 로그아웃
링크, 쿠키 읽기)은 북마크와 같다.

세션: 출처에 맡긴 요청 헤더(쿠키)를 같은 출처의 요청에만 붙인다(infosource._browse와 같다). 사내 SSO 쿠키는
무인 수집을 켰을 때만 맡길 수 있다(config.source_unattended) — 맡긴 것이 없으면 로그인 없이 열리는 사이트만 된다.
사이트가 로그인 화면을 내면 돌지 않고 그렇게 알린다.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .infosource import SourceError, USER_AGENT, _launch, _origin, os_trusted

RUNNER = "http://gpax-bot.invalid"   # 받는 창의 가짜 출처 — 네트워크로 나가지 않는다(가로채 답한다)
ENGINE_DIR = Path(__file__).resolve().parents[2] / "console" / "src" / "lib"
DEFAULT_TIMEOUT = 600.0              # 무인 수집 한 번의 상한(초)

# 받는 창 — 엔진의 인사(hello)에 시작(start)으로 답하고, 기록·결과를 파이썬으로 넘긴다(pages/AgentReceive와 같은 통로).
_RUNNER_HTML = """<!doctype html><meta charset="utf-8"><title>GPAX bot</title><script>
var nonce = Math.random().toString(36).slice(2) + Date.now().toString(36);
var START = __START__;
window.addEventListener('message', function (e) {
  var m = e.data;
  if (!m || typeof m !== 'object' || e.source !== window.opener) return;
  if (m.gpax === 'hello') {
    e.source.postMessage({ gpax: 'start', nonce: nonce, mode: 'replay', source: START.source,
                           queries: START.source.queries, version: START.source.version }, e.origin);
    return;
  }
  if (m.nonce !== nonce) return;
  if (m.gpax === 'log') window.gpaxLog(String(m.text || ''));
  if (m.gpax === 'result') window.gpaxResult(m.payload);
});
window.opener.postMessage({ gpax: 'ready' }, '*');
</script>"""


def engine_available() -> bool:
    return (ENGINE_DIR / "scanAgent.js").is_file() and (ENGINE_DIR / "scanData.js").is_file()


def _engine() -> str:
    """북마크와 같은 식 — lib/agentCode.ts가 웹사이트에서 부르는 모양 그대로(콘솔 주소만 RUNNER로)."""
    if not engine_available():
        raise SourceError(f"수집 엔진 파일이 없습니다: {ENGINE_DIR} — 서버의 소스(console/src/lib)가 필요합니다.")
    agent = (ENGINE_DIR / "scanAgent.js").read_text(encoding="utf-8").strip().rstrip(";")
    data = (ENGINE_DIR / "scanData.js").read_text(encoding="utf-8").strip().rstrip(";")
    peer = json.dumps({"console": RUNNER, "receive": f"{RUNNER}/agent#/agent"})
    return f"({agent})({peer}, {data});"


def run(source: dict, screens: list[dict], headers: dict, *, start_url: str,
        timeout: float = DEFAULT_TIMEOUT, step=lambda _t: None) -> dict:
    """수집 조건(screens)대로 봇을 한 번 돌려 북마크가 콘솔에 보내는 결과(payload)를 돌려준다.

    source는 {id, name} — 결과에 실려 돌아온다(infosource._check_payload가 이 출처의 것인지 본다)."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    origin = _origin(start_url)
    start = {"source": {"id": source["id"], "name": source["name"], "origin": origin,
                        "queries": screens, "version": source.get("version", "")}}
    html = _RUNNER_HTML.replace("__START__", json.dumps(start, ensure_ascii=False).replace("</", "<\\/"))
    box: dict = {}
    landed_title = landed_url = ""
    # 사내 루트 CA — OS가 검증한 인증서만 받는다(infosource.os_trusted)
    trust = os_trusted([start_url, *(s.get("url", "") for s in screens), *(s.get("start", "") for s in screens)])
    with sync_playwright() as pw:
        browser, _name = _launch(pw, trust)
        try:
            context = browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=USER_AGENT,
                                          accept_downloads=False)

            def attach(route, request):
                if _origin(request.url) == origin and headers:
                    route.continue_(headers={**request.headers, **headers})
                else:
                    route.continue_()

            context.route("**/*", attach)
            # 나중에 단 것이 먼저 돈다 — 받는 창 주소는 여기서 답한다(밖으로 나가지 않는다)
            context.route(f"{RUNNER}/**", lambda route, _req: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html))
            context.expose_binding("gpaxLog", lambda _src, text: step(str(text)[:300]))
            context.expose_binding("gpaxResult", lambda _src, payload: box.setdefault("payload", payload))
            # 사이트가 묻는 창(confirm)에는 '아니오' — 엔진도 그렇게 답한다(저장·삭제를 묻는 창을 넘기지 않게)
            context.on("page", lambda p: p.on("dialog", lambda d: d.dismiss()))
            page = context.new_page()
            page.on("dialog", lambda d: d.dismiss())
            step(f"서버 브라우저로 {start_url}을 엽니다")
            page.goto(start_url, wait_until="networkidle", timeout=60_000)
            landed = _origin(page.url)
            if landed != origin:
                raise SourceError(f"다른 주소({landed})로 넘어갔습니다 — 로그인(SSO) 화면입니다. 서버에 맡긴 세션이 없거나 "
                                  "만료됐습니다(무인 수집 설정과 출처의 요청 헤더를 보세요).")
            landed_title, landed_url = page.title()[:80], page.url[:200]
            step(f"열린 화면: {landed_title} · {landed_url}")
            if page.locator("input[type=password]").count():
                raise SourceError("로그인 화면이 떴습니다 — 서버에 맡긴 세션이 없거나 만료됐습니다(무인 수집 설정과 "
                                  "출처의 요청 헤더를 보세요). 북마크로는 로그인한 내 브라우저에서 돌릴 수 있습니다.")
            page.evaluate(_engine())
            deadline = time.monotonic() + timeout
            while "payload" not in box:
                if time.monotonic() > deadline:
                    raise SourceError(f"봇이 {int(timeout)}초 안에 끝나지 않았습니다.")
                page.wait_for_timeout(500)
        finally:
            browser.close()
    payload = box["payload"]
    if not isinstance(payload, dict):
        raise SourceError("봇이 결과를 돌려주지 않았습니다.")
    payload["landed"] = {"title": landed_title, "url": landed_url}   # 진단 — 첫 화면이 무엇이었나
    return payload
