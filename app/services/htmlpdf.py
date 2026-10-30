"""HTML 보고서 → PDF — 스마트워크의 html 보고서를 서버가 인쇄해 PDF로 내려 준다.

글자가 그대로 남는 PDF여야 한다(복사·검색). 그림으로 찍어 PDF에 붙이는 방식(html2canvas)은 글자가 그림이 되고
파일이 커진다 — 브라우저의 인쇄 엔진을 쓴다. 엔진은 둘 중 있는 것: Playwright(선택 의존성 — 데이터 수집이 이미 쓴다)
또는 서버에 깔린 Chrome·Edge(--headless --print-to-pdf). 둘 다 없으면 PdfUnavailable — 화면은 PDF 단추를 감추고
HTML 다운로드만 보인다.

**모델이 쓴 HTML이다.** 화면에서 sandbox로 막는 것을 여기서도 막는다: 스크립트를 끄고, 바깥 자원(네트워크·서버의
file://)을 하나도 읽지 않는다 — CSP(default-src 'none'; 그림은 data:만)를 문서 맨 앞에 두고, Playwright면 모든
요청을 끊는다. 막지 않으면 보고서 HTML이 <img src="file:///…">로 서버의 파일을 PDF에 담아 내보낼 수 있다.
"""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from ..config import get_settings

MAX_HTML_BYTES = 5 * 1024 * 1024
TIMEOUT = 60
# 동시에 띄우는 브라우저 수 — 보고서마다 브라우저 하나라 무겁다.
_slots = threading.BoundedSemaphore(2)

CSP = ('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\';'
       ' img-src data:; font-src data:">')
PRINT_CSS = ("<style>@page { size: A4; margin: 14mm; } html { -webkit-print-color-adjust: exact;"
             " print-color-adjust: exact; }</style>")
_BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


class PdfUnavailable(RuntimeError):
    """서버에 PDF를 만들 브라우저가 없다 — 화면은 HTML 다운로드만 보인다."""


class PdfFailed(RuntimeError):
    """브라우저는 있는데 만들지 못했다(시간 초과 등)."""


def document(html: str, title: str = "") -> str:
    """인쇄할 문서 — CSP와 인쇄 설정을 **맨 앞에**(보고서가 통째 문서여도 head로 들어간다)."""
    safe_title = title.replace("<", "").replace(">", "")[:200]
    return f"{CSP}{PRINT_CSS}<title>{safe_title}</title>{html}"


def browser_path() -> str:
    configured = get_settings().pdf_browser_path.strip().strip('"')
    if configured:
        return configured if Path(configured).is_file() else ""
    for name in ("chrome", "msedge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return next((p for p in _BROWSERS if Path(p).is_file()), "")


def engine() -> str:
    """쓸 엔진 이름 — 설정한 브라우저 > Playwright > 깔린 Chrome·Edge. 없으면 빈 문자열."""
    if get_settings().pdf_browser_path.strip() and browser_path():
        return "browser"
    if importlib.util.find_spec("playwright") is not None:
        return "playwright"
    return "browser" if browser_path() else ""


def _with_playwright(doc: str) -> bytes:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 — 선택 의존성

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_context(java_script_enabled=False).new_page()
            page.route("**/*", lambda route: route.abort())   # 어떤 요청도 나가지 않는다(set_content는 요청이 없다)
            page.set_content(doc, wait_until="load", timeout=TIMEOUT * 1000)
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            browser.close()


def _with_browser(doc: str, exe: str) -> bytes:
    # 스크립트는 문서 맨 앞의 CSP(default-src 'none')가 막는다 — --blink-settings=scriptEnabled=false로 끄면 새
    # headless가 PDF를 아예 쓰지 않는다(실측, Chrome 141).
    with tempfile.TemporaryDirectory(prefix="gpax-pdf-") as tmp:
        src, out = Path(tmp) / "report.html", Path(tmp) / "report.pdf"
        src.write_text(doc, encoding="utf-8")
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
               "--no-pdf-header-footer", "--disable-extensions",
               f"--user-data-dir={Path(tmp) / 'profile'}", f"--print-to-pdf={out}", src.as_uri()]
        try:
            subprocess.run(cmd, capture_output=True, timeout=TIMEOUT, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        except subprocess.TimeoutExpired as e:
            raise PdfFailed(f"PDF를 {TIMEOUT}초 안에 만들지 못했습니다") from e
        if not out.is_file() or out.stat().st_size == 0:
            raise PdfFailed("브라우저가 PDF를 만들지 못했습니다")
        return out.read_bytes()


def render(html: str, title: str = "") -> bytes:
    """HTML 보고서를 PDF로. 엔진이 없으면 PdfUnavailable, 만들다 실패하면 PdfFailed."""
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise PdfFailed(f"보고서가 너무 큽니다({MAX_HTML_BYTES // (1024 * 1024)}MB 초과)")
    kind = engine()
    if not kind:
        raise PdfUnavailable(
            "서버에 PDF를 만들 브라우저가 없습니다(Playwright 또는 Chrome·Edge) — PAAS_PDF_BROWSER_PATH로 지정할 수 있습니다.")
    doc = document(html, title)
    with _slots:
        if kind == "playwright":
            try:
                return _with_playwright(doc)
            except Exception as e:  # noqa: BLE001 — 브라우저 바이너리가 안 깔린 Playwright 등: 깔린 브라우저로
                exe = browser_path()
                if not exe:
                    raise PdfFailed(f"PDF를 만들지 못했습니다: {e}"[:300]) from e
                return _with_browser(doc, exe)
        return _with_browser(doc, browser_path())
