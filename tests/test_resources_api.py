"""대화식 편집 화면 자원 리스팅 — 모듈 카테고리·조직 스코프 + GET /projects/{id}/resources."""
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN = {"x-api-key": "test-admin-key"}


def _client() -> TestClient:
    return TestClient(create_app())


def test_module_create_accepts_category_and_org(fresh_settings):
    c = _client()
    r = c.post("/paas/api/v1/modules", json={
        "name": "news-api", "type": "external_api", "category": "news",
        "config": {"url": "https://news.example.com"},
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["category"] == "news"
    assert r.json()["organization_id"] is None


def test_module_create_rejects_unknown_org(fresh_settings):
    c = _client()
    r = c.post("/paas/api/v1/modules", json={
        "name": "shop-db", "type": "database", "organization_id": 999, "config": {},
    }, headers=ADMIN)
    assert r.status_code == 404


def test_project_resources_filters_by_organization(monkeypatch, fresh_settings):
    from app.config import get_settings
    from app.services import gitea

    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    get_settings.cache_clear()
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: type(
        "R", (), {"status_code": 201, "text": "",
                  "json": lambda self=None: {"clone_url": "https://git.example.com/shop-team/api.git"}}
    )())
    c = _client()
    org_id = c.post("/paas/api/v1/orgs", json={"name": "shop-team"}, headers=ADMIN).json()["id"]

    c.post("/paas/api/v1/modules", json={
        "name": "news-api", "type": "external_api", "category": "news", "config": {},
    }, headers=ADMIN)
    c.post("/paas/api/v1/modules", json={
        "name": "shop-db", "type": "database", "organization_id": org_id,
        "category": None, "config": {},
    }, headers=ADMIN)

    shop_pid = c.post("/paas/api/v1/projects", json={
        "name": "shop-web", "type": "react", "organization_id": org_id,
    }, headers=ADMIN).json()["id"]
    other_pid = c.post("/paas/api/v1/projects", json={
        "name": "other-app", "type": "python", "git_url": "https://git.example.com/y",
    }, headers=ADMIN).json()["id"]

    shop_resources = {r["name"] for r in c.get(f"/paas/api/v1/projects/{shop_pid}/resources", headers=ADMIN).json()}
    assert shop_resources == {"news-api", "shop-db"}

    other_resources = {r["name"] for r in c.get(f"/paas/api/v1/projects/{other_pid}/resources", headers=ADMIN).json()}
    assert other_resources == {"news-api"}


def test_project_resources_unknown_project_404(fresh_settings):
    c = _client()
    assert c.get("/paas/api/v1/projects/999999/resources", headers=ADMIN).status_code == 404
