"""사내 MCP 서버 디렉터리 — 실재하는 서버만, 등록된 것에서 만들어 낸다."""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def _client(monkeypatch, tmp_path, base="http://localhost:7000/paas", doc_roots="") -> TestClient:
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_ROOTS", doc_roots)
    if base:
        monkeypatch.setenv("PAAS_MCP_INTERNAL_BASE_URL", base)
    get_settings.cache_clear()
    return TestClient(create_app())


def test_ops_server_is_always_listed(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    items = c.get(f"{API}/mcp/search", headers=ADMIN).json()
    ops = next(i for i in items if i["id"] == "paas-ops")
    assert ops["url"] == "http://localhost:7000/paas/api/v1/mcp/ops"
    assert ops["vendor"] == "사내(이 플랫폼)"


def test_storage_and_code_servers_come_from_what_exists(monkeypatch, tmp_path, fresh_settings):
    """실재하지 않는 대상을 목록에 올리지 않는다 — 예전 외부 목록이 바로 그 실수였다.

    저장소는 환경변수가 정하므로 내부 저장소 하나는 언제나 있고, 코드 서버는 프로젝트를
    등록해야 생긴다."""
    c = _client(monkeypatch, tmp_path)
    assert [i["id"] for i in c.get(f"{API}/mcp/search", headers=ADMIN).json()] == [
        "paas-ops", "paas-docs", "paas-storage-internal"]

    monkeypatch.setenv("PAAS_DOC_ROOTS", f"company-docs={tmp_path / 'docs'}")
    get_settings.cache_clear()
    pid = c.post(f"{API}/projects", json={
        "name": "shop-web", "type": "react", "git_url": "https://git.example.com/x",
    }, headers=ADMIN).json()["id"]

    items = {i["id"]: i for i in c.get(f"{API}/mcp/search", headers=ADMIN).json()}
    assert items["paas-storage-company-docs"]["url"].endswith("/mcp/storage/company-docs")
    assert items["paas-code-shop-web"]["url"].endswith(f"/mcp/projects/{pid}/code")


def test_db_server_is_listed_only_when_allowlisted(monkeypatch, tmp_path, fresh_settings):
    """허용 목록에 없으면 서버가 403이라 목록에 올리면 안 된다."""
    c = _client(monkeypatch, tmp_path)
    c.post(f"{API}/modules", json={
        "name": "paydb", "type": "database", "config": {"dsn": "sqlite:///x.db"},
    }, headers=ADMIN)
    assert not [i for i in c.get(f"{API}/mcp/search", headers=ADMIN).json()
                if i["id"].startswith("paas-db-")]

    monkeypatch.setenv("PAAS_MCP_DB_MODULES", "paydb")
    get_settings.cache_clear()
    assert [i["id"] for i in c.get(f"{API}/mcp/search", headers=ADMIN).json()
            if i["id"].startswith("paas-db-")] == ["paas-db-paydb"]


def test_keyword_search_narrows(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path, doc_roots=f"company-docs={tmp_path / 'docs'}")
    # "저장소"는 문서 검색 서버 설명에도 들어 있어 셋 다 걸린다
    hits = c.get(f"{API}/mcp/search", params={"q": "저장소"}, headers=ADMIN).json()
    assert [i["id"] for i in hits] == [
        "paas-docs", "paas-storage-internal", "paas-storage-company-docs"]
    # 카테고리로 좁히면 저장소 서버만 남는다
    assert [i["id"] for i in c.get(f"{API}/mcp/search", params={"q": "storage"},
                                   headers=ADMIN).json()] == [
        "paas-storage-internal", "paas-storage-company-docs"]
    # (저장소 이름이 company-docs라 "docs"로는 둘 다 걸린다 — 설명의 낱말로 좁힌다)
    assert [i["id"] for i in c.get(f"{API}/mcp/search", params={"q": "가로질러"},
                                   headers=ADMIN).json()] == ["paas-docs"]
    assert c.get(f"{API}/mcp/search", params={"q": "없는말"}, headers=ADMIN).json() == []


def test_without_a_base_url_entries_carry_no_address(monkeypatch, tmp_path, fresh_settings):
    """동작하지 않을 주소를 만들어 주지 않는다 — 등록은 막히고 경로만 보여 준다."""
    c = _client(monkeypatch, tmp_path, base="")
    ops = c.get(f"{API}/mcp/search", headers=ADMIN).json()[0]
    assert ops["url"] == ""
    assert ops["path"] == "/paas/api/v1/mcp/ops"


def test_backchannel_url_is_the_fallback_base(monkeypatch, tmp_path, fresh_settings):
    """백채널 주소가 바로 '플랫폼이 자기 자신에게 닿는 주소'다."""
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_BACKCHANNEL_URL", "http://10.0.0.5:7000/paas")
    c = _client(monkeypatch, tmp_path, base="")
    ops = c.get(f"{API}/mcp/search", headers=ADMIN).json()[0]
    assert ops["url"] == "http://10.0.0.5:7000/paas/api/v1/mcp/ops"


def test_import_registers_an_internal_server_as_a_module(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    ops = next(i for i in c.get(f"{API}/mcp/search", headers=ADMIN).json() if i["id"] == "paas-ops")
    created = c.post(f"{API}/modules/import-mcp", headers=ADMIN,
                     json={"id": ops["id"], "name": ops["name"], "url": ops["url"],
                           "category": ops["category"]})
    assert created.status_code == 201, created.text
    modules = {m["name"]: m for m in c.get(f"{API}/modules", headers=ADMIN).json()}
    assert modules["paas-ops"]["type"] == "mcp"
    # 사내 주소라 유출 판정도 internal이다
    assert modules["paas-ops"]["egress"]["scope"] == "internal"


def test_refresh_reports_the_current_count(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    body = c.post(f"{API}/modules/search/refresh-mcp", headers=ADMIN).json()
    assert body["total_mcp_servers"] == 3  # ops + docs + 내부 저장소
    assert body["base_url"] == "http://localhost:7000/paas"
