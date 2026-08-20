"""MCP 모듈 연결 확인 — 등록만으로는 동작 여부를 알 수 없다.

주소가 틀렸거나(이름 조회 실패), 전송 방식이 안 맞거나(이 클라이언트는 단일 JSON
응답만 다루므로 /sse 엔드포인트와는 통신 불가), 서버가 stdio 전용이면 등록은 성공한
채 조용히 죽어 있다.
"""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import mcp_client

ADMIN = {"x-api-key": "test-admin-key"}


def _mcp_module(c, url="http://mcp.internal:8000/mcp") -> int:
    return c.post("/paas/api/v1/modules", json={
        "name": "brave", "type": "mcp", "config": {"url": url, "api_key": "k"},
    }, headers=ADMIN).json()["id"]


def test_check_reports_tools_when_server_answers(monkeypatch, fresh_settings):
    get_settings.cache_clear()
    c = TestClient(create_app())
    module_id = _mcp_module(c)
    monkeypatch.setattr(mcp_client, "list_tools",
                        lambda url, api_key=None: [{"name": "brave_web_search"}, {"name": "x"}])

    body = c.post(f"/paas/api/v1/modules/{module_id}/mcp-check", headers=ADMIN).json()
    assert body["ok"] is True
    assert body["tool_count"] == 2
    assert "brave_web_search" in body["tools"]


def test_check_reports_failure_without_raising(monkeypatch, fresh_settings):
    """확인 실패는 오류가 아니라 결과다 — 화면이 여러 모듈을 나열하며 표시한다."""
    get_settings.cache_clear()
    c = TestClient(create_app())
    module_id = _mcp_module(c)

    def _boom(url, api_key=None):
        raise ConnectionError("Name or service not known")

    monkeypatch.setattr(mcp_client, "list_tools", _boom)
    r = c.post(f"/paas/api/v1/modules/{module_id}/mcp-check", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert "Name or service not known" in r.json()["error"]


def test_sse_url_gets_a_transport_hint(fresh_settings):
    """/sse 주소는 이 클라이언트로 통신 자체가 안 된다 — 그 이유가 메시지에 있어야
    "주소는 맞는데 왜 안 되나"를 헤매지 않는다."""
    result = mcp_client.check_server("http://nowhere.invalid:8000/sse")
    assert result["ok"] is False
    assert "SSE" in result["error"]


def test_check_rejects_non_mcp_module(fresh_settings):
    get_settings.cache_clear()
    c = TestClient(create_app())
    module_id = c.post("/paas/api/v1/modules", json={
        "name": "news", "type": "external_api", "config": {"url": "https://x.example.com"},
    }, headers=ADMIN).json()["id"]
    r = c.post(f"/paas/api/v1/modules/{module_id}/mcp-check", headers=ADMIN)
    assert r.status_code == 400


def test_directory_only_offers_this_platforms_own_servers(monkeypatch, tmp_path, fresh_settings):
    """실재하지 않는 주소를 목록으로 내주면 "등록했는데 왜 안 되나"를 추적하게 된다 —
    예전에는 .internal 주소 7건이 하드코딩돼 있었고 그 중 무엇도 실재하지 않았다.

    지금 목록은 이 플랫폼이 직접 노출하는 서버에서만 만들어지므로, 모든 항목의 주소가
    설정된 기준 주소로 시작해야 한다."""
    from app.db import SessionLocal
    from app.main import create_app
    from app.services import mcp_search

    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("PAAS_MCP_INTERNAL_BASE_URL", "http://localhost:7000/paas")
    from app.config import get_settings

    get_settings.cache_clear()
    create_app()
    session = SessionLocal()
    try:
        entries = mcp_search.list_internal_servers(session)
    finally:
        session.close()

    assert entries, "적어도 운영 조회 서버는 항상 있어야 한다"
    for item in entries:
        assert item["url"].startswith("http://localhost:7000/paas/api/v1/mcp/"), item
        assert item["path"].startswith("/paas/api/v1/mcp/"), item
