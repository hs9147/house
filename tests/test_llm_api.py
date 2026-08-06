"""LLM/모듈 API 통합 — 프로바이더 키 마스킹·조직 범위, 리뷰 엔드포인트."""
import json

from fastapi.testclient import TestClient

from app.main import create_app
from app.services import llm as llm_service

ADMIN = {"x-api-key": "test-admin-key"}


def _client() -> TestClient:
    return TestClient(create_app())


def _create_provider(c: TestClient, name="claude") -> int:
    r = c.post("/paas/api/v1/llm/providers", json={
        "name": name, "kind": "openai", "base_url": "https://api.example.com",
        "api_key": "sk-secret", "model": "test-model",
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_project(c: TestClient, name="editor-target") -> int:
    r = c.post("/paas/api/v1/projects", json={
        "name": name, "type": "python", "git_url": "https://git.example.com/org/x",
    }, headers=ADMIN)
    return r.json()["id"]


def test_provider_key_never_exposed():
    c = _client()
    _create_provider(c)
    listing = c.get("/paas/api/v1/llm/providers", headers=ADMIN).json()
    assert listing[0]["has_api_key"] is True
    assert "sk-secret" not in str(listing)


def test_internal_provider_must_use_project_scheme():
    """보안수정 — kind=internal인데 base_url이 외부 URL이면 등록 자체를 거부한다."""
    c = _client()
    r = c.post("/paas/api/v1/llm/providers", json={
        "name": "fake-internal", "kind": "internal",
        "base_url": "https://api.some-external-llm.com", "model": "m",
    }, headers=ADMIN)
    assert r.status_code == 422
    assert "project://" in r.text


def test_internal_provider_with_project_scheme_succeeds():
    c = _client()
    r = c.post("/paas/api/v1/llm/providers", json={
        "name": "llm-main", "kind": "internal", "base_url": "project://llm-main", "model": "m",
    }, headers=ADMIN)
    assert r.status_code == 201


def _member_key(c: TestClient, name="dev1") -> dict:
    key = c.post("/paas/api/v1/keys", json={"name": name}, headers=ADMIN).json()["key"]
    return {"x-api-key": key}


def _mock_gitea(monkeypatch):
    """조직/조직 소속 프로젝트 생성이 거치는 gitea.ensure_org/ensure_repo를 목킹한다."""
    from app.config import get_settings
    from app.services import gitea

    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    get_settings.cache_clear()
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: type(
        "R", (), {"status_code": 201, "text": "",
                  "json": lambda self=None: {"clone_url": "https://git.example.com/o/r.git"}}
    )())
    monkeypatch.setattr(gitea.httpx, "get", lambda url, **kw: type(
        "R", (), {"status_code": 404, "text": ""}
    )())


def test_non_admin_key_allowed_for_global_external_provider_session():
    """전역(organization_id 미지정) 외부 프로바이더는 모든 사용자가 쓸 수 있다."""
    c = _client()
    pid = _create_project(c)
    prov = _create_provider(c)  # external, organization_id 미지정 = 전역
    r = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
               headers=_member_key(c))
    assert r.status_code == 201


def test_non_admin_key_allowed_for_internal_provider_session():
    c = _client()
    pid = _create_project(c)
    prov_id = c.post("/paas/api/v1/llm/providers", json={
        "name": "llm-internal", "kind": "internal", "base_url": "project://llm-internal",
        "model": "m",
    }, headers=ADMIN).json()["id"]
    r = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov_id},
               headers=_member_key(c))
    assert r.status_code == 201


def test_org_scoped_provider_blocked_for_other_org_project(monkeypatch, fresh_settings):
    """조직 지정 프로바이더는 그 조직 소속 프로젝트에서만 쓸 수 있다(Module과 동일 규칙)."""
    _mock_gitea(monkeypatch)
    c = _client()
    org_a = c.post("/paas/api/v1/orgs", json={"name": "org-a"}, headers=ADMIN).json()["id"]
    org_b = c.post("/paas/api/v1/orgs", json={"name": "org-b"}, headers=ADMIN).json()["id"]
    prov = c.post("/paas/api/v1/llm/providers", json={
        "name": "org-a-only", "kind": "openai", "base_url": "https://api.example.com",
        "model": "m", "organization_id": org_a,
    }, headers=ADMIN).json()["id"]
    other_org_project = c.post("/paas/api/v1/projects", json={
        "name": "org-b-project", "type": "python", "organization_id": org_b,
    }, headers=ADMIN).json()["id"]

    r = c.post("/paas/api/v1/plan/sessions",
               json={"project_id": other_org_project, "provider_id": prov}, headers=_member_key(c))
    assert r.status_code == 403
    assert "org-a-only" in r.text


def test_org_scoped_provider_allowed_for_same_org_project(monkeypatch, fresh_settings):
    _mock_gitea(monkeypatch)
    c = _client()
    org_a = c.post("/paas/api/v1/orgs", json={"name": "org-c"}, headers=ADMIN).json()["id"]
    prov = c.post("/paas/api/v1/llm/providers", json={
        "name": "org-c-only", "kind": "openai", "base_url": "https://api.example.com",
        "model": "m", "organization_id": org_a,
    }, headers=ADMIN).json()["id"]
    same_org_project = c.post("/paas/api/v1/projects", json={
        "name": "org-c-project", "type": "python", "organization_id": org_a,
    }, headers=ADMIN).json()["id"]

    r = c.post("/paas/api/v1/plan/sessions",
               json={"project_id": same_org_project, "provider_id": prov}, headers=_member_key(c))
    assert r.status_code == 201


def test_admin_key_bypasses_provider_org_scope(monkeypatch, fresh_settings):
    _mock_gitea(monkeypatch)
    c = _client()
    org_a = c.post("/paas/api/v1/orgs", json={"name": "org-d"}, headers=ADMIN).json()["id"]
    prov = c.post("/paas/api/v1/llm/providers", json={
        "name": "org-d-only", "kind": "openai", "base_url": "https://api.example.com",
        "model": "m", "organization_id": org_a,
    }, headers=ADMIN).json()["id"]
    pid = _create_project(c)  # organization_id 없음(전역) — org-d와 불일치
    r = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov}, headers=ADMIN)
    assert r.status_code == 201


def test_review_endpoint_with_explicit_diff(monkeypatch):
    monkeypatch.setattr(
        llm_service, "_post_chat",
        lambda url, headers, payload: {"choices": [{"message": {"content":
            '[{"severity": "medium", "file": "a.py", "comment": "예외 처리 누락"}]'
        }}]},
    )
    c = _client()
    pid = _create_project(c)
    prov = _create_provider(c)
    r = c.post(f"/paas/api/v1/projects/{pid}/review",
               json={"provider_id": prov, "diff": "--- a/a.py\n+++ b/a.py\n"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["max_severity"] == "medium"


def test_module_bind_and_llm_context():
    c = _client()
    pid = _create_project(c)
    r = c.post("/paas/api/v1/modules", json={
        "name": "mail", "type": "external_api",
        "config": {"url": "https://cho-fam.web.app/api/mail", "api_key": "mk-1"},
    }, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["config"]["api_key"] == "•••"
    mid = r.json()["id"]

    r = c.post(f"/paas/api/v1/projects/{pid}/modules/{mid}/bind", json={"env_prefix": "MAIL"}, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["injected_env"] == ["MAIL_API_KEY", "MAIL_URL"]

    # 같은 prefix 재사용 금지
    assert c.post(f"/paas/api/v1/projects/{pid}/modules/{mid}/bind",
                  json={"env_prefix": "MAIL"}, headers=ADMIN).status_code == 409

    # 컨텍스트는 A2A Agent Card와 같은 모양이다 — 모델이 카드에서 본 이름으로 그대로 호출한다.
    ctx = c.get(f"/paas/api/v1/projects/{pid}/modules", headers=ADMIN).json()
    assert len(ctx) == 1
    card = ctx[0]
    assert card["agent_name"] == "mail"
    assert card["type"] == "external_api"
    assert card["env_prefix"] == "MAIL"
    assert card["skills"] == ["invoke_api", "fetch_data"]
    assert card["paas_a2a_endpoint"] == "/paas/api/v1/a2a/agents/mail/task"
    # 비밀값은 카드에 실리지 않는다
    assert "mk-1" not in json.dumps(card)


def test_llm_module_type_can_be_created_and_bound():
    """llm 타입 모듈 — 배포 앱이 직접 쓸 LLM 엔드포인트(URL/API_KEY/MODEL 주입)."""
    c = _client()
    pid = _create_project(c)
    r = c.post("/paas/api/v1/modules", json={
        "name": "shop-llm", "type": "llm",
        "config": {"url": "https://api.example.com/v1", "api_key": "sk-1", "model": "gpt-4o"},
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    mid = r.json()["id"]

    r = c.post(f"/paas/api/v1/projects/{pid}/modules/{mid}/bind", json={"env_prefix": "LLM"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["injected_env"] == ["LLM_API_KEY", "LLM_MODEL", "LLM_URL"]

    card = c.get(f"/paas/api/v1/projects/{pid}/modules", headers=ADMIN).json()[0]
    assert card["type"] == "llm"
    assert card["skills"] == ["chat_completion"]
    assert "sk-1" not in json.dumps(card)


def test_unbind_module_removes_only_that_binding():
    """모듈 바인딩 해제는 이 바인딩만 지운다 — 모듈 정의나 다른 바인딩은 남는다."""
    c = _client()
    pid = _create_project(c)
    other_pid = _create_project(c, name="other-target")
    mid = c.post("/paas/api/v1/modules", json={
        "name": "mail2", "type": "external_api",
        "config": {"url": "https://svc.example.com", "api_key": "mk-2"},
    }, headers=ADMIN).json()["id"]

    c.post(f"/paas/api/v1/projects/{pid}/modules/{mid}/bind", json={"env_prefix": "MAIL"}, headers=ADMIN)
    c.post(f"/paas/api/v1/projects/{other_pid}/modules/{mid}/bind", json={"env_prefix": "MAIL"}, headers=ADMIN)
    binding_id = c.get(f"/paas/api/v1/projects/{pid}/modules", headers=ADMIN).json()[0]["binding_id"]
    other_binding_id = c.get(f"/paas/api/v1/projects/{other_pid}/modules", headers=ADMIN).json()[0]["binding_id"]

    # 다른 프로젝트의 바인딩 id로 해제 시도 → 404 (프로젝트 소속 검증)
    assert c.delete(f"/paas/api/v1/projects/{pid}/modules/bindings/{other_binding_id}",
                    headers=ADMIN).status_code == 404

    r = c.delete(f"/paas/api/v1/projects/{pid}/modules/bindings/{binding_id}", headers=ADMIN)
    assert r.status_code == 204
    assert c.get(f"/paas/api/v1/projects/{pid}/modules", headers=ADMIN).json() == []

    # 다른 프로젝트의 바인딩과 모듈 정의는 그대로
    assert len(c.get(f"/paas/api/v1/projects/{other_pid}/modules", headers=ADMIN).json()) == 1
    assert c.get("/paas/api/v1/modules", headers=ADMIN).json()

    # 이미 지운 바인딩을 다시 지우면 404
    assert c.delete(f"/paas/api/v1/projects/{pid}/modules/bindings/{binding_id}",
                    headers=ADMIN).status_code == 404
