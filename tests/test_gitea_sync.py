"""Gitea → 플랫폼 조직/프로젝트 동기화 — services/gitea_sync.py, POST /orgs/sync."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import Organization, Project
from app.services import gitea, gitea_sync

ADMIN = {"x-api-key": "test-admin-key"}


@pytest.fixture(autouse=True)
def _configured(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    monkeypatch.setenv("PAAS_GITEA_API_TOKEN", "tok-123")
    get_settings.cache_clear()
    create_app()  # Base.metadata.create_all(engine) — SessionLocal을 직접 여는 테스트용
    yield
    get_settings.cache_clear()


def test_sync_creates_missing_org_and_python_project(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(gitea, "list_orgs", lambda: [{"username": "acme"}])
    monkeypatch.setattr(
        gitea, "list_org_repos",
        lambda org: [{"name": "billing-api", "clone_url": "https://git.example.com/acme/billing-api.git",
                      "default_branch": "main"}],
    )

    def fake_clone(git_url, branch):
        d = tmp_path_factory.mktemp("billing-api")
        (d / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
        return d

    monkeypatch.setattr(gitea_sync, "_shallow_clone", fake_clone)

    with SessionLocal() as db:
        result = gitea_sync.sync_from_gitea(db)
        assert result["orgs_created"] == ["acme"]
        assert result["projects_created"] == ["billing-api"]
        assert result["skipped"] == []

        org = db.execute(select(Organization).where(Organization.name == "acme")).scalar_one()
        project = db.execute(select(Project).where(Project.name == "billing-api")).scalar_one()
        assert project.organization_id == org.id
        assert project.type.value == "python"
        assert project.git_url == "https://git.example.com/acme/billing-api.git"
        assert project.branch == "main"


def test_sync_reuses_existing_org_skips_existing_project(monkeypatch):
    with SessionLocal() as db:
        org = Organization(name="acme")
        db.add(org)
        db.commit()
        db.add(Project(name="already-here", type="python", organization_id=org.id,
                        git_url="https://git.example.com/acme/already-here.git"))
        db.commit()

    monkeypatch.setattr(gitea, "list_orgs", lambda: [{"username": "acme"}])
    monkeypatch.setattr(
        gitea, "list_org_repos",
        lambda org_name: [{"name": "already-here", "clone_url": "x", "default_branch": "main"}],
    )
    monkeypatch.setattr(
        gitea_sync, "_shallow_clone",
        lambda *a: (_ for _ in ()).throw(AssertionError("should not clone an already-known repo")),
    )

    with SessionLocal() as db:
        result = gitea_sync.sync_from_gitea(db)
        assert result["orgs_created"] == []  # 이미 있던 조직 재사용
        assert result["projects_created"] == []
        assert result["skipped"] == []
        assert len(db.execute(select(Organization)).scalars().all()) == 1  # 중복 생성 없음


def test_sync_skips_project_when_type_unrecognizable(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(gitea, "list_orgs", lambda: [{"username": "acme"}])
    monkeypatch.setattr(
        gitea, "list_org_repos",
        lambda org: [{"name": "mystery", "clone_url": "https://git.example.com/acme/mystery.git",
                      "default_branch": "main"}],
    )
    monkeypatch.setattr(gitea_sync, "_shallow_clone", lambda *a: tmp_path_factory.mktemp("mystery"))

    with SessionLocal() as db:
        result = gitea_sync.sync_from_gitea(db)
        assert result["projects_created"] == []
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["name"] == "mystery"
        assert result["skipped"][0]["kind"] == "project"
        assert "추론" in result["skipped"][0]["reason"]


def test_sync_skips_invalid_names(monkeypatch):
    monkeypatch.setattr(gitea, "list_orgs", lambda: [{"username": "Bad_Org"}])
    monkeypatch.setattr(gitea, "list_org_repos", lambda org: [])

    with SessionLocal() as db:
        result = gitea_sync.sync_from_gitea(db)
        assert result["orgs_created"] == []
        assert result["skipped"] == [{"name": "Bad_Org", "kind": "org", "reason": "이름 규칙에 맞지 않음"}]


def test_sync_skips_project_on_clone_failure(monkeypatch):
    monkeypatch.setattr(gitea, "list_orgs", lambda: [{"username": "acme"}])
    monkeypatch.setattr(
        gitea, "list_org_repos",
        lambda org: [{"name": "unreachable", "clone_url": "https://git.example.com/acme/unreachable.git",
                      "default_branch": "main"}],
    )

    def fail_clone(*a):
        raise RuntimeError("fatal: repository not found")

    monkeypatch.setattr(gitea_sync, "_shallow_clone", fail_clone)

    with SessionLocal() as db:
        result = gitea_sync.sync_from_gitea(db)
        assert result["projects_created"] == []
        assert result["skipped"][0]["name"] == "unreachable"
        assert "clone 실패" in result["skipped"][0]["reason"]


def test_sync_recreates_missing_repo_by_default(monkeypatch):
    """플랫폼엔 있지만 Gitea에 리포가 없는 조직 소속 프로젝트는 기본값(create)에서
    Gitea에 리포를 다시 만들고 git_url을 갱신한다 — 프로젝트는 지우지 않는다."""
    with SessionLocal() as db:
        org = Organization(name="acme")
        db.add(org)
        db.commit()
        db.add(Project(name="orphaned", type="python", organization_id=org.id,
                        git_url="https://git.example.com/acme/orphaned.git"))
        db.commit()

    monkeypatch.setattr(gitea, "list_orgs", lambda: [{"username": "acme"}])
    monkeypatch.setattr(gitea, "list_org_repos", lambda org_name: [])  # Gitea엔 리포가 없음
    ensure_calls = []
    monkeypatch.setattr(
        gitea, "ensure_repo",
        lambda org_name, repo_name: (ensure_calls.append((org_name, repo_name)),
                                      "https://git.example.com/acme/orphaned.git")[1],
    )

    with SessionLocal() as db:
        result = gitea_sync.sync_from_gitea(db)  # on_missing_repo 기본값 "create"
        assert result["repos_created"] == ["orphaned"]
        assert result["projects_deleted"] == []
        assert ensure_calls == [("acme", "orphaned")]

        project = db.execute(select(Project).where(Project.name == "orphaned")).scalar_one()
        assert project.git_url == "https://git.example.com/acme/orphaned.git"


def test_sync_deletes_project_when_requested(monkeypatch):
    """on_missing_repo="delete"면 리포를 되살리지 않고 플랫폼 쪽 프로젝트와 딸린
    행(배포 이력 등)을 지운다."""
    from app.models import Deployment

    with SessionLocal() as db:
        org = Organization(name="acme")
        db.add(org)
        db.commit()
        project = Project(name="orphaned", type="python", organization_id=org.id,
                           git_url="https://git.example.com/acme/orphaned.git")
        db.add(project)
        db.commit()
        db.add(Deployment(project_id=project.id, git_sha="a" * 40, image_tag="orphaned:a",
                          profile="release", status="running"))
        db.commit()
        project_id = project.id

    monkeypatch.setattr(gitea, "list_orgs", lambda: [{"username": "acme"}])
    monkeypatch.setattr(gitea, "list_org_repos", lambda org_name: [])
    monkeypatch.setattr(
        gitea, "ensure_repo",
        lambda *a: (_ for _ in ()).throw(AssertionError("delete 모드에선 리포를 만들면 안 됨")),
    )

    class _FakeRuntime:
        def stop(self, *a):
            pass

    from app.services import deployer
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())

    with SessionLocal() as db:
        result = gitea_sync.sync_from_gitea(db, on_missing_repo="delete")
        assert result["projects_deleted"] == ["orphaned"]
        assert result["repos_created"] == []

        assert db.execute(select(Project).where(Project.id == project_id)).scalar_one_or_none() is None
        assert db.execute(
            select(Deployment).where(Deployment.project_id == project_id)
        ).scalar_one_or_none() is None


def test_sync_ignores_legacy_project_without_organization(monkeypatch):
    """git_url을 직접 지정한(조직 없는) 프로젝트는 Gitea 관리 대상이 아니므로
    "리포 없음" 판정 자체를 받지 않는다."""
    with SessionLocal() as db:
        db.add(Project(name="legacy", type="python", git_url="https://github.com/org/legacy"))
        db.commit()

    monkeypatch.setattr(gitea, "list_orgs", lambda: [])
    monkeypatch.setattr(
        gitea, "ensure_repo",
        lambda *a: (_ for _ in ()).throw(AssertionError("레거시 프로젝트는 건드리면 안 됨")),
    )

    with SessionLocal() as db:
        result = gitea_sync.sync_from_gitea(db)
        assert result["repos_created"] == []
        assert result["projects_deleted"] == []
        assert result["skipped"] == []


def test_sync_endpoint_admin_only(monkeypatch):
    monkeypatch.setattr(gitea, "list_orgs", lambda: [])
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/orgs/sync", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "orgs_created": [], "projects_created": [], "repos_created": [],
        "projects_deleted": [], "skipped": [],
    }

    member_key = c.post("/paas/api/v1/keys", json={"name": "dev"}, headers=ADMIN).json()["key"]
    r2 = c.post("/paas/api/v1/orgs/sync", headers={"x-api-key": member_key})
    assert r2.status_code == 403


def test_sync_endpoint_maps_not_configured_to_503(fresh_settings, monkeypatch):
    monkeypatch.delenv("PAAS_GITEA_URL", raising=False)
    get_settings.cache_clear()
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/orgs/sync", headers=ADMIN)
    assert r.status_code == 503
