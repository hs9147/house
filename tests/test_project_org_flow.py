"""프로젝트-조직 연동 — organization_id 지정 시 Gitea 리포 내부 생성,
git_url이 비관리자 응답에서 마스킹되는지 검증."""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import gitea

ADMIN = {"x-api-key": "test-admin-key"}


def _client_with_org(monkeypatch) -> tuple[TestClient, int]:
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    get_settings.cache_clear()
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: type(
        "R", (), {"status_code": 201, "text": "",
                  "json": lambda self=None: {"clone_url": "https://git.example.com/shop-team/api.git"}}
    )())
    c = TestClient(create_app())
    org_id = c.post("/paas/api/v1/orgs", json={"name": "shop-team"}, headers=ADMIN).json()["id"]
    return c, org_id


def test_project_with_org_creates_repo_internally(monkeypatch, fresh_settings):
    c, org_id = _client_with_org(monkeypatch)
    r = c.post("/paas/api/v1/projects", json={
        "name": "shop-api", "type": "python", "organization_id": org_id,
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    # admin은 실제 git_url을 본다
    assert r.json()["git_url"] == "https://git.example.com/shop-team/api.git"
    assert r.json()["organization_id"] == org_id


def test_project_with_org_and_git_url_rejected(fresh_settings):
    get_settings.cache_clear()
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/projects", json={
        "name": "bad", "type": "python", "organization_id": 1,
        "git_url": "https://github.com/x/y",
    }, headers=ADMIN)
    assert r.status_code == 422


def test_project_without_org_or_git_url_rejected(fresh_settings):
    get_settings.cache_clear()
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/projects", json={"name": "bad", "type": "python"}, headers=ADMIN)
    assert r.status_code == 422


def test_git_url_masked_for_non_admin(monkeypatch, fresh_settings):
    c, org_id = _client_with_org(monkeypatch)
    member = c.post("/paas/api/v1/keys", json={"name": "dev1"}, headers=ADMIN).json()["key"]

    r = c.post("/paas/api/v1/projects", json={
        "name": "shop-web", "type": "react", "organization_id": org_id,
    }, headers={"x-api-key": member})
    assert r.status_code == 201
    assert r.json()["git_url"] == "(내부 관리 — 관리자만 조회 가능)"

    listing = c.get("/paas/api/v1/projects", headers={"x-api-key": member}).json()
    assert all(p["git_url"] == "(내부 관리 — 관리자만 조회 가능)" for p in listing)

    # admin 목록에서는 동일 프로젝트가 실제 URL로 보여야 함
    admin_listing = c.get("/paas/api/v1/projects", headers=ADMIN).json()
    shop_web = next(p for p in admin_listing if p["name"] == "shop-web")
    assert shop_web["git_url"] == "https://git.example.com/shop-team/api.git"


def test_git_url_visible_to_user_account_in_the_same_organization(monkeypatch, fresh_settings):
    """API 키(조직 개념 없음)와 달리, 계정 로그인 사용자는 소속 조직 프로젝트의
    git 주소를 조회할 수 있다 — 관리자가 아니어도 된다."""
    c, org_id = _client_with_org(monkeypatch)

    project_id = c.post("/paas/api/v1/projects", json={
        "name": "shop-billing", "type": "python", "organization_id": org_id,
    }, headers=ADMIN).json()["id"]

    # 가입 → 승인 → 로그인 → shop-team 조직 배지 부여
    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "")
    get_settings.cache_clear()
    c.post("/paas/api/v1/auth/register",
          json={"email": "dev@shop.com", "name": "dev", "password": "pw12345"})
    account_id = next(a["id"] for a in c.get("/paas/api/v1/auth/accounts", headers=ADMIN).json()
                      if a["email"] == "dev@shop.com")
    c.post(f"/paas/api/v1/auth/accounts/{account_id}/approve", headers=ADMIN)
    c.post(f"/paas/api/v1/auth/accounts/{account_id}/organizations/modify",
          json={"organization_id": org_id, "action": "add"}, headers=ADMIN)
    session_key = c.post("/paas/api/v1/auth/login",
                         json={"email": "dev@shop.com", "password": "pw12345"}).json()["key"]

    listing = c.get("/paas/api/v1/projects", headers={"x-api-key": session_key}).json()
    shop_billing = next(p for p in listing if p["id"] == project_id)
    assert shop_billing["git_url"] == "https://git.example.com/shop-team/api.git"

    # 소속이 없는 다른 조직 프로젝트는 여전히 마스킹된다
    other_org_id = c.post("/paas/api/v1/orgs", json={"name": "other-team"}, headers=ADMIN).json()["id"]
    other_project_id = c.post("/paas/api/v1/projects", json={
        "name": "other-app", "type": "python", "organization_id": other_org_id,
    }, headers=ADMIN).json()["id"]
    listing2 = c.get("/paas/api/v1/projects", headers={"x-api-key": session_key}).json()
    other_app = next(p for p in listing2 if p["id"] == other_project_id)
    assert other_app["git_url"] == "(내부 관리 — 관리자만 조회 가능)"

    # 조직 미지정(전역) 프로젝트는 누구나 본다
    global_project_id = c.post("/paas/api/v1/projects", json={
        "name": "global-tool", "type": "python", "git_url": "https://github.com/org/global-tool",
    }, headers=ADMIN).json()["id"]
    listing3 = c.get("/paas/api/v1/projects", headers={"x-api-key": session_key}).json()
    global_tool = next(p for p in listing3 if p["id"] == global_project_id)
    assert global_tool["git_url"] == "https://github.com/org/global-tool"


def test_plan_repo_clone_url_visible_to_user_account_in_the_same_organization(monkeypatch, fresh_settings):
    """개발도구 연동용 리포 정보(/plan/sessions/{id}/repo)도 프로젝트 git_url과 같은
    규칙을 따른다 — 기획을 진행하는 소속 사용자가 clone_url을 못 보면 화면이 무용하다."""
    c, org_id = _client_with_org(monkeypatch)
    project_id = c.post("/paas/api/v1/projects", json={
        "name": "shop-plan", "type": "python", "organization_id": org_id,
    }, headers=ADMIN).json()["id"]
    provider_id = c.post("/paas/api/v1/llm/providers", json={
        "name": "p", "kind": "openai", "base_url": "https://api.example.com",
        "api_key": "sk-secret", "model": "m",
    }, headers=ADMIN).json()["id"]
    session_id = c.post("/paas/api/v1/plan/sessions",
                        json={"project_id": project_id, "provider_id": provider_id},
                        headers=ADMIN).json()["id"]

    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "")
    get_settings.cache_clear()
    c.post("/paas/api/v1/auth/register",
          json={"email": "planner@shop.com", "name": "planner", "password": "pw12345"})
    account_id = next(a["id"] for a in c.get("/paas/api/v1/auth/accounts", headers=ADMIN).json()
                      if a["email"] == "planner@shop.com")
    c.post(f"/paas/api/v1/auth/accounts/{account_id}/approve", headers=ADMIN)
    c.post(f"/paas/api/v1/auth/accounts/{account_id}/organizations/modify",
          json={"organization_id": org_id, "action": "add"}, headers=ADMIN)
    session_key = c.post("/paas/api/v1/auth/login",
                         json={"email": "planner@shop.com", "password": "pw12345"}).json()["key"]

    repo = c.get(f"/paas/api/v1/plan/sessions/{session_id}/repo",
                headers={"x-api-key": session_key}).json()
    assert repo["clone_url"] == "https://git.example.com/shop-team/api.git"

    # 순수 API 키(조직 개념 없음)에게는 여전히 마스킹(None)이다
    member = c.post("/paas/api/v1/keys", json={"name": "svc1"}, headers=ADMIN).json()["key"]
    repo2 = c.get(f"/paas/api/v1/plan/sessions/{session_id}/repo",
                 headers={"x-api-key": member}).json()
    assert repo2["clone_url"] is None


def test_legacy_project_without_organization_still_works(fresh_settings):
    get_settings.cache_clear()
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/projects", json={
        "name": "legacy-app", "type": "python", "git_url": "https://github.com/org/legacy",
    }, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["git_url"] == "https://github.com/org/legacy"
    assert r.json()["organization_id"] is None
