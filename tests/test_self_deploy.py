"""콘솔 자기 배포 부트스트랩(옵트인, PAAS_SELF_DEPLOY_CONSOLE) — services/self_deploy.py."""
from sqlalchemy import select

from app.db import SessionLocal
from app.main import create_app
from app.models import BuildProfile, Deployment, Organization, Project, ProjectType
from app.services import deployer, self_deploy


def _project_row(db):
    return db.execute(
        select(Project).where(Project.name == self_deploy.SELF_CONSOLE_PROJECT_NAME)
    ).scalar_one_or_none()


def test_noop_when_disabled():
    create_app()
    self_deploy.bootstrap_console_deploy()  # PAAS_SELF_DEPLOY_CONSOLE 기본값 false
    with SessionLocal() as db:
        assert _project_row(db) is None


def test_noop_when_git_url_missing(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_SELF_DEPLOY_CONSOLE", "true")
    monkeypatch.delenv("PAAS_SELF_DEPLOY_CONSOLE_GIT_URL", raising=False)
    create_app()
    self_deploy.bootstrap_console_deploy()
    with SessionLocal() as db:
        assert _project_row(db) is None


def test_noop_when_git_policy_blocks(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_SELF_DEPLOY_CONSOLE", "true")
    monkeypatch.setenv("PAAS_SELF_DEPLOY_CONSOLE_GIT_URL", "https://github.com/org/chofam")
    monkeypatch.setenv("PAAS_GIT_INTERNAL_ONLY", "true")
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.internal.example.com")
    create_app()
    self_deploy.bootstrap_console_deploy()
    with SessionLocal() as db:
        assert _project_row(db) is None


def test_creates_project_and_triggers_deploy_once(monkeypatch, fresh_settings):
    """create_app()이 이미 부트스트랩을 한 번 태우므로(app/main.py), 여기서는
    create_app() 호출 전에 deploy_queued를 미리 mock해 그 첫 호출을 관찰한다."""
    monkeypatch.setenv("PAAS_SELF_DEPLOY_CONSOLE", "true")
    monkeypatch.setenv("PAAS_SELF_DEPLOY_CONSOLE_GIT_URL", "https://git.example.com/hs9147/chofam")
    monkeypatch.setenv("PAAS_SELF_DEPLOY_CONSOLE_BRANCH", "main")
    monkeypatch.setenv("PAAS_GIT_INTERNAL_ONLY", "false")  # 테스트 기본값과 동일하게 명시

    calls = []
    monkeypatch.setattr(
        deployer, "deploy_queued",
        lambda db, project, profile, git_sha=None: calls.append((project.name, profile)),
    )

    create_app()  # app/main.py가 "deploy" 기능 활성 시 bootstrap_console_deploy()를 호출

    with SessionLocal() as db:
        project = _project_row(db)
        assert project is not None
        assert project.type == ProjectType.react
        assert project.source_subdir == self_deploy.SELF_CONSOLE_SUBDIR
        assert project.git_url == "https://git.example.com/hs9147/chofam"
        assert project.branch == "main"
        assert project.organization_id is not None
        org = db.get(Organization, project.organization_id)
        assert org.name == self_deploy.SELF_DEPLOY_ORG_NAME  # "admin" — /apps/admin/paas-console/
    assert calls == [(self_deploy.SELF_CONSOLE_PROJECT_NAME, BuildProfile.release)]
    # 재배포 skip(idempotent) 동작은 test_second_bootstrap_call_skips_redeploy에서
    # 실제 deploy_queued(Deployment 행을 동기 생성)를 통해 검증한다 — 여기서 쓴
    # deploy_queued mock은 DB에 아무것도 안 남기므로 그 판단에는 부적합하다.


def test_second_bootstrap_call_skips_redeploy(monkeypatch, fresh_settings):
    """이미 배포 이력(Deployment 행)이 있으면 재기동 때마다 다시 빌드하지 않는다.
    checkout을 실패로 stub해(빠른 실패) 실제 git clone/네트워크 호출 없이도
    deploy_queued가 Deployment 행을 동기적으로 만드는 것만 확인한다."""
    monkeypatch.setenv("PAAS_SELF_DEPLOY_CONSOLE", "true")
    monkeypatch.setenv("PAAS_SELF_DEPLOY_CONSOLE_GIT_URL", "https://git.example.com/hs9147/chofam")
    monkeypatch.setenv("PAAS_GIT_INTERNAL_ONLY", "false")
    monkeypatch.setattr(
        deployer, "checkout", lambda project, git_sha=None: (_ for _ in ()).throw(RuntimeError("stub"))
    )

    create_app()  # 첫 부트스트랩 — 실제 deploy_queued 실행, Deployment 행 동기 생성

    with SessionLocal() as db:
        project = _project_row(db)
        assert db.execute(
            select(Deployment.id).where(Deployment.project_id == project.id)
        ).first() is not None

    calls = []
    monkeypatch.setattr(
        deployer, "deploy_queued",
        lambda db, project, profile, git_sha=None: calls.append(1),
    )
    self_deploy.bootstrap_console_deploy()
    assert calls == []
