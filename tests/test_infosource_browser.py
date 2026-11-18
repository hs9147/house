"""서버 스캔의 브라우저(Playwright)를 실제로 띄운다 — 로컬 HTTP 서버의 화면을 그리고, 열지 못한 화면이 있으면
실패 기록(trace)을 남기는지. 브라우저를 띄울 수 없는 환경(내장 Chromium·Edge·Chrome 모두 없음)이면 건너뛴다."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import get_settings
from app.services import infosource

PAGES = {
    "/": "<html><head><title>첫 화면</title></head><body><nav><a href='/notice'>공지</a></nav>"
         "<div id='menu'></div><script>document.getElementById('menu').innerHTML="
         "\"<a href='/drawn'>그린 메뉴</a>\"</script></body></html>",
    "/notice": "<html><head><title>공지</title></head><body><h1>공지 목록</h1>"
               "<table><tr><th>번호</th><th>제목</th></tr><tr><td>1</td><td>휴가</td></tr></table></body></html>",
}


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = PAGES.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write((body or "없음").encode("utf-8"))

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def browser_ok():
    if not infosource.browser_available():
        pytest.skip("playwright 없음")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser, _ = infosource._launch(pw)
        except infosource.SourceError as e:
            pytest.skip(str(e))
        browser.close()


def test_browse_renders_shots_pages_and_a_trace_on_failure(site, browser_ok, monkeypatch, tmp_path, fresh_settings):
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    broken = "http://127.0.0.1:1/"   # 열리지 않는다
    out = infosource._browse(7, [f"{site}/", broken], {}, site, extra=[f"{site}/notice"])
    by_url = {p["url"]: p for p in out["pages"]}
    assert "그린 메뉴" in by_url[f"{site}/"]["html"]            # 스크립트가 그린 메뉴까지
    assert (infosource._shot_dir(7) / "0.jpg").is_file()
    assert "shot" not in by_url[f"{site}/notice"]               # 「페이지」로 받으려고 그렸다
    assert "| 1 | 휴가 |" in infosource._markdown(by_url[f"{site}/notice"]["html"])
    assert by_url[broken].get("error")
    assert out["trace"] is True and infosource.trace_path(infosource.InfoSource(id=7)) is not None

    # 헤더(세션)를 단 출처는 trace를 남기지 않는다 — trace에 요청 헤더가 실린다
    out = infosource._browse(7, [broken], {"Cookie": "s=1"}, site)
    assert out["trace"] is False and infosource.trace_path(infosource.InfoSource(id=7)) is None


def test_read_page_tool_renders_and_blocks_other_hosts(site, browser_ok, monkeypatch, fresh_settings):
    """paas-browser — 허용한 호스트(127.0.0.1)는 그려 읽고, 화면이 부르는 다른 호스트(localhost)의 자원은 막는다."""
    from app.services import webbrowse

    PAGES["/tool"] = ("<html><head><title>도구</title><script src='http://localhost:9/x.js'></script></head><body>"
                      "<h1>읽기</h1><div id='d'></div><script>document.getElementById('d').textContent='그린 글'</script>"
                      "<a href='/notice'>공지</a><a href='http://localhost:9/y'>밖</a></body></html>")
    monkeypatch.setenv("PAAS_BROWSER_ALLOWED_HOSTS", "127.0.0.1")
    monkeypatch.setenv("PAAS_BROWSER_APPROVED", "true")
    get_settings.cache_clear()
    webbrowse._used.clear()
    page = webbrowse.read_page("t", f"{site}/tool")
    assert page["status"] == 200 and page["title"] == "도구"
    assert page["markdown"].startswith("# 읽기") and "그린 글" in page["markdown"]
    assert [link["url"] for link in page["links"]] == [f"{site}/notice"]
    assert page["blocked"] == ["localhost"]


SEARCH = ("<html><head><title>조회</title></head><body><label for='y'>연도</label><input id='y' placeholder='YYYY.MM.DD'>"
          "<button onclick=\"document.getElementById('r').innerHTML='<table><tr><th>날짜</th></tr><tr><td>'"
          "+document.getElementById('y').value+'</td></tr></table>'\">{label}</button><button>저장</button>"
          "<div id='r'></div></body></html>")


def test_actor_asks_the_llm_once_then_replays_without_it(site, browser_ok, monkeypatch, tmp_path, fresh_settings):
    """조건이 있는 화면 — 처음에는 LLM이 접근성 트리를 보고 칸·버튼을 찾아 저장하고, 다음부터는 LLM 없이 채운다.
    화면이 바뀌어 저장한 것이 맞지 않으면 다시 묻는다. LLM에는 칸 이름만 가고 값은 가지 않는다."""
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    PAGES["/search"] = SEARCH.format(label="조회")
    prompts = []

    def chat(provider, messages, db):
        text = messages[0]["content"]
        prompts.append(text)
        label = "검색" if 'button "검색"' in text else "조회"
        return ('{"fill": [{"i": 0, "role": "textbox", "name": "연도"}], '
                f'"submit": {{"role": "button", "name": "{label}"}}}}')

    monkeypatch.setattr(infosource.llm, "default_provider", lambda db, organization_id=None: object())
    monkeypatch.setattr(infosource.llm, "chat_completion", chat)
    url = f"{site}/search"
    screen = {"screen": "기간 조회", "url": url, "conditions": [{"field": "등록일", "value": "@last|@today"}],
              "last": "2026-10-01"}

    def run():
        notes = []
        actor = infosource._Actor(None, 9, {url: screen}, notes)
        out = infosource._browse(9, [], {}, site, extra=[url], prepare=actor)
        actor.save()
        return out["pages"][0], notes

    page, notes = run()
    assert "<td>2026.10.01</td>" in page["html"]                 # 지난 수집일을 그 칸의 모양(점)으로
    assert len(prompts) == 1 and 'textbox "연도"' in prompts[0] and "2026" not in prompts[0]
    assert any("저장했습니다" in n for n in notes)
    assert infosource._actions_file(9).is_file()

    page, notes = run()
    assert "<td>2026.10.01</td>" in page["html"] and len(prompts) == 1 and notes == []   # LLM 없이

    PAGES["/search"] = SEARCH.format(label="검색")                 # 화면이 바뀌었다
    page, notes = run()
    assert "<td>2026.10.01</td>" in page["html"] and len(prompts) == 2
    assert any("맞지 않아" in n for n in notes)

    monkeypatch.setattr(infosource.llm, "chat_completion", lambda *a: (
        '{"fill": [{"i": 0, "role": "textbox", "name": "연도"}], "submit": {"role": "button", "name": "저장"}}'))
    infosource._actions_file(9).unlink()
    page, _ = run()
    assert "LLM이 조건 칸" in page["error"]                         # 바꾸는 버튼은 누르지 않는다


def _tls_site(tmp_path):
    """사내 루트 CA를 흉내 낸다 — 이 시험만의 CA가 서명한 127.0.0.1 인증서로 HTTPS를 연다."""
    import datetime
    import ipaddress
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GPAX test root")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(2)
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                         serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "leaf.pem", tmp_path / "key.pem")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"https://127.0.0.1:{server.server_address[1]}"


def test_browser_accepts_only_the_certificate_the_os_store_verified(browser_ok, monkeypatch, tmp_path):
    """사내 루트 CA를 브라우저가 모를 때(ERR_CERT_AUTHORITY_INVALID) — OS 저장소가 검증한 서버 인증서의 공개키만
    브라우저에 넘겨 받게 한다. OS도 모르는 인증서면 아무것도 넘기지 않는다(검증을 끄지 않는다)."""
    import ssl

    from playwright.sync_api import sync_playwright

    server, url = _tls_site(tmp_path)
    try:
        assert infosource.os_trusted([f"{url}/"]) == []          # OS 저장소가 모르는 CA — 넘기지 않는다
        assert infosource.os_trusted(["http://127.0.0.1:1/"]) == []
        real = ssl.create_default_context
        monkeypatch.setattr(infosource.ssl, "create_default_context",
                            lambda *a, **kw: real(cafile=str(tmp_path / "ca.pem")))   # OS 저장소에 사내 CA가 있다
        trust = infosource.os_trusted([f"{url}/", f"{url}/notice"])
        assert len(trust) == 1
        with sync_playwright() as pw:
            for pins, ok in (([], False), (trust, True)):
                browser, _ = infosource._launch(pw, pins)
                try:
                    page = browser.new_page()
                    try:
                        page.goto(f"{url}/notice", timeout=15000)
                        assert ok and page.title() == "공지"
                    except Exception as e:  # noqa: BLE001
                        assert not ok and "ERR_CERT" in str(e), e
                finally:
                    browser.close()
    finally:
        server.shutdown()
