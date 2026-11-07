"""웹 페이지 읽기(services/webbrowse · MCP /mcp/browser, 「paas-browser」) — 읽기만, 허용한 호스트만.

보는 것: 허용 목록(하위 도메인 포함, 비슷한 이름은 아님)·검증 전에는 꺼져 있는지·한 시간 상한, MCP 응답과 감사(호스트·
글자 수만), 설정했을 때만 서버 목록에 나오는지. 실제 브라우저로는 tests/test_infosource_browser.py가 본다."""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import webbrowse

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


@pytest.fixture
def on(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_BROWSER_ALLOWED_HOSTS", "lge.co.kr, .data.go.kr")
    monkeypatch.setenv("PAAS_BROWSER_APPROVED", "true")
    monkeypatch.setenv("PAAS_BROWSER_MAX_PER_HOUR", "2")
    get_settings.cache_clear()
    monkeypatch.setattr(webbrowse.infosource, "browser_available", lambda: True)
    webbrowse._used.clear()


def test_only_allowed_hosts_and_their_subdomains(on):
    assert webbrowse.host_allowed("lge.co.kr") and webbrowse.host_allowed("www.lge.co.kr")
    assert webbrowse.host_allowed("WWW.DATA.GO.KR.")
    assert not webbrowse.host_allowed("evil-lge.co.kr") and not webbrowse.host_allowed("lge.co.kr.evil.test")
    assert not webbrowse.host_allowed("gpax.lge.com") and not webbrowse.host_allowed("")
    with pytest.raises(webbrowse.BrowseError, match="허용하지 않은 호스트.*lge.co.kr"):
        webbrowse._check("a", "http://10.0.0.1/admin")
    with pytest.raises(webbrowse.BrowseError, match="http·https"):
        webbrowse._check("a", "file:///etc/passwd")


def test_off_until_hosts_are_set_and_review_is_done(on, monkeypatch):
    assert webbrowse.state() == "on"
    monkeypatch.setenv("PAAS_BROWSER_APPROVED", "false")
    get_settings.cache_clear()
    assert webbrowse.state() == "pending"
    with pytest.raises(webbrowse.BrowseError, match="검증 전"):
        webbrowse._check("a", "https://www.lge.co.kr/")
    monkeypatch.setenv("PAAS_BROWSER_APPROVED", "true")
    monkeypatch.setenv("PAAS_BROWSER_ALLOWED_HOSTS", "")
    get_settings.cache_clear()
    assert webbrowse.state() == "no_hosts"


def test_hourly_limit_per_caller(on):
    webbrowse._check("a", "https://www.lge.co.kr/")
    webbrowse._check("a", "https://www.lge.co.kr/")
    with pytest.raises(webbrowse.BrowseError, match="한 시간에 2번"):
        webbrowse._check("a", "https://www.lge.co.kr/")
    webbrowse._check("b", "https://www.lge.co.kr/")


def test_mcp_browser_server_and_audit(on, monkeypatch):
    seen = []

    def fake_read(actor, url):
        seen.append((actor, url))
        if "nope" in url:
            raise webbrowse.BrowseError("허용하지 않은 호스트입니다: nope.test")
        return {"url": url, "status": 200, "title": "제품", "markdown": "# 제품\n\n비밀-본문", "truncated": False,
                "links": [], "blocked": ["cdn.other.test"]}

    monkeypatch.setattr(webbrowse, "read_page", fake_read)
    c = TestClient(create_app())

    def call(args):
        return c.post(f"{API}/mcp/browser", headers=ADMIN, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "read_web_page", "arguments": args}}).json()

    ok = call({"url": "https://www.lge.co.kr/tv"})
    assert "# 제품" in ok["result"]["content"][0]["text"]
    assert "허용하지 않은" in call({"url": "https://nope.test/"})["error"]["message"]

    from app.db import SessionLocal
    from app.models import AuditEvent
    with SessionLocal() as s:
        event = s.query(AuditEvent).filter(AuditEvent.action == "mcp.browser.read").one()
        assert event.target == "www.lge.co.kr" and event.detail["blocked"] == 1
        assert "비밀-본문" not in str(event.detail) and "/tv" not in str(event.detail)


def test_listed_only_when_on(on, monkeypatch):
    c = TestClient(create_app())
    ids = lambda: [s["id"] for s in c.get(f"{API}/mcp/search", headers=ADMIN).json()]  # noqa: E731
    assert "paas-browser" in ids()
    monkeypatch.setenv("PAAS_BROWSER_APPROVED", "false")
    get_settings.cache_clear()
    assert "paas-browser" not in ids()
