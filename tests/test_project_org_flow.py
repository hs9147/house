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


def test_legacy_project_without_organization_still_works(fresh_settings):
    get_settings.cache_clear()
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/projects", json={
        "name": "legacy-app", "type": "python", "git_url": "https://github.com/org/legacy",
    }, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["git_url"] == "https://github.com/org/legacy"
    assert r.json()["organization_id"] is None
