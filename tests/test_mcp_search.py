"""사내 MCP 서버 디렉터리 — 실재하는 서버만, 등록된 것에서 만들어 낸다."""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import mcp_client

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


def _import_ops(c) -> dict:
    ops = next(i for i in c.get(f"{API}/mcp/search", headers=ADMIN).json() if i["id"] == "paas-ops")
    created = c.post(f"{API}/modules/import-mcp", headers=ADMIN,
                     json={"id": ops["id"], "name": ops["name"], "url": ops["url"],
                           "category": ops["category"]})
    assert created.status_code == 201, created.text
    return created.json()


def test_import_registers_an_internal_server_as_a_module(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    _import_ops(c)
    modules = {m["name"]: m for m in c.get(f"{API}/modules", headers=ADMIN).json()}
    assert modules["paas-ops"]["type"] == "mcp"
    # 사내 주소라 유출 판정도 internal이다
    assert modules["paas-ops"]["egress"]["scope"] == "internal"


def test_imported_internal_server_gets_a_key_that_actually_opens_it(
        monkeypatch, tmp_path, fresh_settings):
    """키 없이 등록하면 등록은 성공한 채 연결 확인이 401로 떨어진다 — 원클릭 등록이
    동작하지 않는 모듈을 만들어 내는 셈이다(실제로 그랬다)."""
    c = _client(monkeypatch, tmp_path)
    created = _import_ops(c)
    assert created["key_issued"] is True
    assert created["config"]["api_key"] == "•••"  # 값은 가려서 내보낸다

    sent: dict[str, str] = {}

    def post_rpc(url, headers, payload):
        """mcp_client의 HTTP 경계를 이 앱으로 되돌린다 — 발급된 키가 실제로 통하는지 본다."""
        sent.update(headers)
        res = c.post(url.replace("http://localhost:7000/paas", "/paas"),
                     headers=headers, json=payload)
        res.raise_for_status()
        return res.json()

    monkeypatch.setattr(mcp_client, "_post_rpc", post_rpc)
    body = c.post(f"{API}/modules/{created['id']}/mcp-check", headers=ADMIN).json()
    assert body["ok"] is True, body["error"]
    assert body["tool_count"] > 0

    # 발급 키는 **비관리자**여야 한다 — mcp 모듈의 api_key는 바인딩된 앱의 환경변수로도
    # 주입되므로, 관리자 키를 넣으면 관리자 권한이 앱 env로 새어 나간다.
    raw = sent["authorization"].removeprefix("Bearer ")
    assert c.get(f"{API}/audit", headers={"x-api-key": raw}).status_code == 403


def test_external_server_import_does_not_issue_a_key(monkeypatch, tmp_path, fresh_settings):
    """사외 서버의 자격증명을 플랫폼이 지어낼 수는 없다 — 빈 채로 두고 사용자가 넣는다."""
    c = _client(monkeypatch, tmp_path)
    created = c.post(f"{API}/modules/import-mcp", headers=ADMIN, json={
        "id": "vendor", "name": "vendor-mcp", "url": "https://mcp.vendor.com/v1",
        "category": "etc"}).json()
    assert created["key_issued"] is False


def test_a_keyless_module_says_why_it_is_401(monkeypatch, tmp_path, fresh_settings):
    """401을 그대로 내주면 "주소가 틀렸나"를 먼저 의심하게 된다."""
    c = _client(monkeypatch, tmp_path)
    created = _import_ops(c)
    # 예전 방식으로 등록된 모듈(키 없음)을 재현한다
    c.put(f"{API}/modules/{created['id']}", headers=ADMIN, json={
        "name": "paas-ops", "type": "mcp",
        "config": {"url": created["config"]["url"], "api_key": ""}})

    def post_rpc(url, headers, payload):
        res = c.post(url.replace("http://localhost:7000/paas", "/paas"),
                     headers=headers, json=payload)
        res.raise_for_status()
        return res.json()

    monkeypatch.setattr(mcp_client, "_post_rpc", post_rpc)
    body = c.post(f"{API}/modules/{created['id']}/mcp-check", headers=ADMIN).json()
    assert body["ok"] is False
    assert "API 키가 없습니다" in body["error"]
    # 고치는 방법이 곧 잃는 방법이면 안내가 아니다 — 지우라고 하지 않는다
    assert "키 발급" in body["error"]
    assert "지울 필요 없습니다" in body["error"]


def test_refresh_reports_the_current_count(monkeypatch, tmp_path, fresh_settings):
    c = _client(monkeypatch, tmp_path)
    body = c.post(f"{API}/modules/search/refresh-mcp", headers=ADMIN).json()
    assert body["total_mcp_servers"] == 3  # ops + docs + 내부 저장소
    assert body["base_url"] == "http://localhost:7000/paas"


def test_issue_key_fixes_a_keyless_module_in_place(monkeypatch, tmp_path, fresh_settings):
    """지우지 않고 고칠 수 있어야 한다.

    자동 발급은 '사내 MCP 검색'으로 가져올 때만 걸린다. 그 전에 등록됐거나 주소를 직접
    적어 만든 모듈은 키가 빈 채로 남는다. 예전 안내는 **모듈을 지우고 다시 가져오라**고
    했는데, 바인딩된 프로젝트가 있으면 그럴 수 없다 — 고치는 방법이 곧 잃는 방법이면
    안내가 아니다.
    """
    c = _client(monkeypatch, tmp_path)
    created = _import_ops(c)
    # 예전 방식으로 등록된 모듈(키 없음)을 재현한다
    c.put(f"{API}/modules/{created['id']}", headers=ADMIN, json={
        "name": "paas-ops", "type": "mcp",
        "config": {"url": created["config"]["url"], "api_key": ""}})

    before = c.post(f"{API}/modules/{created['id']}/mcp-check", headers=ADMIN).json()
    assert before["ok"] is False and before["can_issue_key"] is True

    issued = c.post(f"{API}/modules/{created['id']}/mcp-key", headers=ADMIN)
    assert issued.status_code == 200, issued.text
    assert issued.json()["key_issued"] is True
    assert issued.json()["config"]["api_key"] == "•••"  # 값은 가려서 내보낸다

    sent: dict[str, str] = {}

    def post_rpc(url, headers, payload):
        sent.update(headers)
        res = c.post(url.replace("http://localhost:7000/paas", "/paas"),
                     headers=headers, json=payload)
        res.raise_for_status()
        return res.json()

    monkeypatch.setattr(mcp_client, "_post_rpc", post_rpc)
    after = c.post(f"{API}/modules/{created['id']}/mcp-check", headers=ADMIN).json()
    assert after["ok"] is True, after["error"]

    # 발급 키는 **비관리자**여야 한다 — 앱 환경변수로도 주입되기 때문이다.
    raw = sent["authorization"].removeprefix("Bearer ")
    assert c.get(f"{API}/audit", headers={"x-api-key": raw}).status_code == 403


def test_issue_key_refuses_an_external_url_and_says_what_the_base_is(
        monkeypatch, tmp_path, fresh_settings):
    """사외 주소에 플랫폼 키를 붙이면 남의 서버로 키를 보내는 꼴이다.

    거절할 때 기준 주소를 함께 알려 준다 — 사내 서버인데 걸렸다면 그 설정이 틀린
    것이고, 값이 보여야 바로잡을 수 있다.
    """
    c = _client(monkeypatch, tmp_path)
    created = c.post(f"{API}/modules/import-mcp", headers=ADMIN, json={
        "id": "vendor", "name": "vendor-mcp", "url": "https://mcp.vendor.com/v1",
        "category": "etc"}).json()
    res = c.post(f"{API}/modules/{created['id']}/mcp-key", headers=ADMIN)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "mcp.vendor.com" in detail
    assert "localhost:7000" in detail  # 지금 기준 주소를 그대로 보여 준다


def test_issue_key_requires_admin(monkeypatch, tmp_path, fresh_settings):
    """이 키는 사내 MCP 서버를 여는 자격증명이다 — 발급은 관리자만."""
    c = _client(monkeypatch, tmp_path)
    created = _import_ops(c)
    raw = c.post(f"{API}/keys", headers=ADMIN,
                 json={"name": "worker", "is_admin": False}).json()["key"]
    res = c.post(f"{API}/modules/{created['id']}/mcp-key", headers={"x-api-key": raw})
    assert res.status_code == 403
