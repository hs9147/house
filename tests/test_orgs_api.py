"""조직 API — 생성(admin, Gitea org 연동)·조회·권한. 프로젝트 생성 연동은
test_project_org_flow.py에서 검증."""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import gitea

ADMIN = {"x-api-key": "test-admin-key"}


def _configure_gitea(monkeypatch):
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    get_settings.cache_clear()


def _mock_org_ok(monkeypatch):
    monkeypatch.setattr(gitea.httpx, "post", lambda url, **kw: type(
        "R", (), {"status_code": 201, "text": "", "json": lambda self=None: {}}
    )())


def test_create_org_requires_admin(monkeypatch, fresh_settings):
    _configure_gitea(monkeypatch)
    _mock_org_ok(monkeypatch)
    c = TestClient(create_app())
    member = c.post("/paas/api/v1/keys", json={"name": "m"}, headers=ADMIN).json()["key"]
    r = c.post("/paas/api/v1/orgs", json={"name": "shop-team"}, headers={"x-api-key": member})
    assert r.status_code == 403


def test_create_org_calls_gitea_and_persists(monkeypatch, fresh_settings):
    _configure_gitea(monkeypatch)
    calls = []
    monkeypatch.setattr(
        gitea.httpx, "post",
        lambda url, **kw: (calls.append(url), type(
            "R", (), {"status_code": 201, "text": "", "json": lambda self=None: {}}
        )())[1],
    )
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/orgs", json={"name": "shop-team"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json() == {"id": 1, "name": "shop-team", "project_count": 0,
                        "created_at": r.json()["created_at"]}
    assert calls == ["https://git.example.com/api/v1/orgs"]

    listing = c.get("/paas/api/v1/orgs", headers=ADMIN).json()
    assert listing == [{"id": 1, "name": "shop-team", "project_count": 0,
                        "created_at": listing[0]["created_at"]}]


def test_create_org_duplicate_rejected(monkeypatch, fresh_settings):
    _configure_gitea(monkeypatch)
    _mock_org_ok(monkeypatch)
    c = TestClient(create_app())
    c.post("/paas/api/v1/orgs", json={"name": "dup-team"}, headers=ADMIN)
    r = c.post("/paas/api/v1/orgs", json={"name": "dup-team"}, headers=ADMIN)
    assert r.status_code == 409


def test_create_org_without_gitea_configured_gives_503(fresh_settings):
    get_settings.cache_clear()
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/orgs", json={"name": "no-gitea"}, headers=ADMIN)
    assert r.status_code == 503
