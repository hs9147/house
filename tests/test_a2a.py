"""A2A 게이트웨이 — 카드 정규화, 디스커버리 조직 스코프, 자격증명 브로커링, 도구 노출."""
import json

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import a2a as a2a_service

ADMIN = {"x-api-key": "test-admin-key"}


def _client() -> TestClient:
    return TestClient(create_app())


def _module(c: TestClient, name="mail", type="external_api", **extra) -> int:
    body = {
        "name": name, "type": type,
        "config": {"url": "https://svc.example.com", "api_key": "mk-1"},
    }
    body.update(extra)
    r = c.post("/paas/api/v1/modules", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_card_normalizes_type_into_skills():
    c = _client()
    _module(c, name="orders-db", type="database")
    card = c.get("/paas/api/v1/a2a/agents/orders-db/card", headers=ADMIN).json()
    assert card["agent_name"] == "orders-db"
    assert card["skills"] == ["execute_query", "inspect_schema"]
    # 분류 태그는 capabilities에 남고, 호출 가능한 verb는 skills에만 있다
    assert "capability.database" in card["capabilities"]
    assert "scope.general" in card["capabilities"]


def test_card_hides_credentials_and_target_url():
    c = _client()
    _module(c)
    card = c.get("/paas/api/v1/a2a/agents/mail/card", headers=ADMIN).json()
    text = json.dumps(card)
    assert "mk-1" not in text
    assert "svc.example.com" not in text
    assert card["paas_a2a_endpoint"] == "/paas/api/v1/a2a/agents/mail/task"


def test_unknown_agent_is_404():
    c = _client()
    assert c.get("/paas/api/v1/a2a/agents/nope/card", headers=ADMIN).status_code == 404
    assert c.post("/paas/api/v1/a2a/agents/nope/task", json={}, headers=ADMIN).status_code == 404


def test_agent_without_endpoint_is_400():
    c = _client()
    c.post("/paas/api/v1/modules", json={
        "name": "no-url", "type": "internal_api", "config": {"note": "미설정"},
    }, headers=ADMIN)
    r = c.post("/paas/api/v1/a2a/agents/no-url/task", json={"capability": "invoke"}, headers=ADMIN)
    assert r.status_code == 400
    assert "endpoint" in r.text


def test_discovery_lists_agents_with_filters():
    c = _client()
    _module(c, name="mail")
    _module(c, name="orders-db", type="database", category="data")

    names = [a["agent_name"] for a in c.get("/paas/api/v1/a2a/agents", headers=ADMIN).json()]
    assert sorted(names) == ["mail", "orders-db"]

    only_db = c.get("/paas/api/v1/a2a/agents?type=database", headers=ADMIN).json()
    assert [a["agent_name"] for a in only_db] == ["orders-db"]

    by_category = c.get("/paas/api/v1/a2a/agents?category=data", headers=ADMIN).json()
    assert [a["agent_name"] for a in by_category] == ["orders-db"]


def test_discovery_hides_other_orgs_agents(monkeypatch, fresh_settings):
    """조직 전용 모듈은 그 조직 프로젝트 관점에서만 보인다 — 전역 조회에는 나오지 않는다."""
    from app.config import get_settings
    from app.services import gitea

    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    get_settings.cache_clear()
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: type(
        "R", (), {"status_code": 201, "text": "",
                  "json": lambda self=None: {"clone_url": "https://git.example.com/team-a/app.git"}}
    )())

    c = _client()
    oid = c.post("/paas/api/v1/orgs", json={"name": "team-a"}, headers=ADMIN).json()["id"]
    _module(c, name="team-secret-db", type="database", organization_id=oid)
    _module(c, name="global-mail")

    listed = [a["agent_name"] for a in c.get("/paas/api/v1/a2a/agents", headers=ADMIN).json()]
    assert listed == ["global-mail"]

    pid = c.post("/paas/api/v1/projects", json={
        "name": "team-app", "type": "python", "organization_id": oid,
    }, headers=ADMIN).json()["id"]
    scoped = c.get(f"/paas/api/v1/a2a/agents?project_id={pid}", headers=ADMIN).json()
    assert sorted(a["agent_name"] for a in scoped) == ["global-mail", "team-secret-db"]


def test_relay_attaches_target_credential_and_never_returns_it(monkeypatch):
    """호출자는 대상의 키를 보지 못한다 — 게이트웨이가 복호화해 대신 붙인다."""
    seen = {}

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"ok": True}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            seen["url"] = url
            seen["json"] = json
            seen["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    c = _client()
    _module(c)
    r = c.post("/paas/api/v1/a2a/agents/mail/task",
               json={"capability": "invoke_api", "input": {"to": "x@y.z"}}, headers=ADMIN)
    assert r.status_code == 200

    assert seen["headers"]["authorization"] == "Bearer mk-1"
    assert seen["headers"]["x-paas-calling-agent"] == "bootstrap-admin"
    assert seen["json"]["capability"] == "invoke_api"
    assert seen["json"]["params"] == {"to": "x@y.z"}
    assert "mk-1" not in r.text

    audit = c.get("/paas/api/v1/audit", headers=ADMIN).json()
    assert any(row["action"] == "a2a.task.execute" and row["target"] == "mail" for row in audit)


def test_build_openai_tools_exposes_one_tool_per_agent_with_skill_enum():
    cards = [
        {"agent_name": "orders-db", "type": "database", "description": "주문 DB",
         "skills": ["execute_query", "inspect_schema"]},
        {"agent_name": "mail-svc", "type": "external_api", "description": "메일",
         "skills": ["invoke_api", "fetch_data"]},
    ]
    tools, registry = a2a_service.build_openai_tools(cards)
    assert [t["function"]["name"] for t in tools] == ["a2a__orders-db", "a2a__mail-svc"]
    params = tools[0]["function"]["parameters"]
    assert params["properties"]["capability"]["enum"] == ["execute_query", "inspect_schema"]
    assert params["required"] == ["capability"]
    assert registry["a2a__mail-svc"]["agent_name"] == "mail-svc"


def test_tool_executor_returns_text_on_failure():
    """도구 실패가 채팅 전체를 끊지 않는다 — mcp_client와 같은 규약."""
    execute = a2a_service.make_tool_executor(None, {}, "caller")
    assert execute("a2a__nope", {}) == "unknown tool: a2a__nope"
