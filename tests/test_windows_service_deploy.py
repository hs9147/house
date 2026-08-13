"""windows_service 런타임에서 deploy_sync가 docker build를 건너뛰고, start.cmd를
조건 없이 자동 생성 + 환경설정(npm/pip install)을 거친 뒤 네이티브 런타임으로
기동하는지 검증."""
import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import BuildProfile, DeploymentStatus, Project, ProjectType
from app.services import deployer
from app.services.build import START_SCRIPT_NAME, BuildError
from app.services.runtime.base import Endpoint


@pytest.fixture(autouse=True)
def _init_db():
    create_app()  # Base.metadata.create_all — 이 파일은 TestClient 없이 직접 세션을 연다


class _FakeRuntime:
    def __init__(self):
        self.calls = []

    def start(self, spec):
        self.calls.append(spec)
        return Endpoint(host="127.0.0.1", port=9101)

    def stop(self, *a): ...
    def status(self, *a): return "running"
    def logs(self, *a, **kw): return ""


def test_windows_service_deploy_skips_build_and_generates_start_script(
    monkeypatch, fresh_settings, tmp_path,
):
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    get_settings.cache_clear()

    workdir = tmp_path / "chatbot"
    workdir.mkdir()
    (workdir / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    monkeypatch.setattr(deployer, "checkout", lambda project, git_sha=None: (workdir, "a" * 40))

    # windows_service 경로는 docker build를 절대 호출하면 안 된다.
    def _boom(*a, **kw):
        raise AssertionError("windows_service는 docker build를 호출하면 안 된다")
    monkeypatch.setattr(deployer, "build_image", _boom)

    # 실제 npm/pip install은 느리고 네트워크가 필요하다 — 호출 여부·인자만 확인한다.
    install_calls = []
    monkeypatch.setattr(deployer, "install_dependencies",
                        lambda wd, log_path, base_path=None: install_calls.append((wd, log_path, base_path)))

    runtime = _FakeRuntime()
    monkeypatch.setattr(deployer, "get_runtime", lambda: runtime)
    monkeypatch.setattr(deployer.proxy, "configure", lambda *a, **kw: None)

    db = SessionLocal()
    try:
        project = Project(name="chatbot", type=ProjectType.python, git_url="https://git.example.com/x")
        db.add(project)
        db.commit()
        db.refresh(project)

        record = deployer.deploy_sync(db, project, BuildProfile.release)

        assert record.status == DeploymentStatus.running
        assert record.image_tag == ""  # 네이티브 실행 — 이미지 없음
        script = workdir / START_SCRIPT_NAME
        assert script.exists()  # 템플릿으로 생성됨
        assert "uvicorn" in script.read_text(encoding="utf-8")
        assert len(runtime.calls) == 1
        # 환경설정이 헬스체크(runtime.start) 전에 끝났고, 로그 경로가 기록됐다
        assert len(install_calls) == 1 and install_calls[0][0] == workdir
        assert record.build_log_path == str(install_calls[0][1])
    finally:
        db.close()


def test_windows_service_deploy_commits_build_log_path_before_install_starts(
    monkeypatch, fresh_settings, tmp_path,
):
    """build_log_path는 install_dependencies가 끝난 뒤가 아니라 부르기 *전*에 커밋돼야
    한다 — 그래야 설치가 오래 걸리거나 멈춰 있는 동안에도(아직 install_dependencies가
    반환하지 않은 시점에) 다른 세션이 그 경로로 진행 중 로그를 조회할 수 있다."""
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    get_settings.cache_clear()

    workdir = tmp_path / "chatbot-order"
    workdir.mkdir()
    (workdir / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    monkeypatch.setattr(deployer, "checkout", lambda project, git_sha=None: (workdir, "d" * 40))
    monkeypatch.setattr(deployer, "build_image", lambda *a, **kw: pytest.fail("build 금지"))
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(deployer.proxy, "configure", lambda *a, **kw: None)

    db = SessionLocal()
    try:
        project = Project(name="chatbot-order", type=ProjectType.python, git_url="https://git.example.com/x")
        db.add(project)
        db.commit()
        db.refresh(project)
        project_id = project.id

        observed = {}

        def _observe_during_install(wd, log_path, base_path=None):
            # install_dependencies가 아직 반환하기 전에 "다른 세션"으로 같은 레코드를
            # 읽어, 그 시점에 이미 build_log_path가 커밋돼 있는지 확인한다.
            reader = SessionLocal()
            try:
                from app.models import Deployment
                rec = reader.query(Deployment).filter_by(project_id=project_id).one()
                observed["build_log_path"] = rec.build_log_path
                observed["status"] = rec.status
            finally:
                reader.close()
        monkeypatch.setattr(deployer, "install_dependencies", _observe_during_install)

        record = deployer.deploy_sync(db, project, BuildProfile.release)

        assert observed["status"] == DeploymentStatus.building  # 그 시점엔 아직 진행 중
        assert observed["build_log_path"] == record.build_log_path
        assert observed["build_log_path"] is not None
    finally:
        db.close()


def test_windows_service_deploy_fails_when_dependency_install_fails(
    monkeypatch, fresh_settings, tmp_path,
):
    """환경설정이 실패하면 배포도 실패로 남는다 — start.cmd 안에서 조용히 넘어가던 것과 다르다."""
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    get_settings.cache_clear()

    workdir = tmp_path / "chatbot3"
    workdir.mkdir()
    monkeypatch.setattr(deployer, "checkout", lambda project, git_sha=None: (workdir, "c" * 40))
    monkeypatch.setattr(deployer, "build_image", lambda *a, **kw: pytest.fail("build 금지"))

    def _fail_install(wd, log_path, base_path=None):
        raise BuildError("pip install 실패 (exit 1)", log_path)
    monkeypatch.setattr(deployer, "install_dependencies", _fail_install)

    started = []
    monkeypatch.setattr(deployer, "get_runtime",
                        lambda: type("R", (), {"start": lambda self, spec: started.append(spec)})())
    monkeypatch.setattr(deployer.proxy, "configure", lambda *a, **kw: None)

    db = SessionLocal()
    try:
        project = Project(name="chatbot3", type=ProjectType.python, git_url="https://git.example.com/x")
        db.add(project)
        db.commit()
        db.refresh(project)

        with pytest.raises(BuildError):
            deployer.deploy_sync(db, project, BuildProfile.release)

        from app.models import Deployment
        record = db.query(Deployment).filter_by(project_id=project.id).one()
        assert record.status == DeploymentStatus.failed
        assert "pip install" in record.error
        assert started == []  # 설치가 실패하면 서비스 기동 자체를 시도하지 않는다
    finally:
        db.close()


def test_windows_service_deploy_regenerates_start_script_unconditionally(
    monkeypatch, fresh_settings, tmp_path,
):
    """start.cmd는 조건 없이 자동 생성한다 — 기존 파일이 있어도 매 배포 시 덮어쓴다."""
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "windows_service")
    get_settings.cache_clear()

    workdir = tmp_path / "chatbot2"
    workdir.mkdir()
    (workdir / START_SCRIPT_NAME).write_text("@echo custom start\n", encoding="utf-8")
    monkeypatch.setattr(deployer, "checkout", lambda project, git_sha=None: (workdir, "b" * 40))
    monkeypatch.setattr(deployer, "build_image", lambda *a, **kw: pytest.fail("build 금지"))
    monkeypatch.setattr(deployer, "install_dependencies", lambda wd, log_path, base_path=None: None)
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(deployer.proxy, "configure", lambda *a, **kw: None)

    db = SessionLocal()
    try:
        project = Project(name="chatbot2", type=ProjectType.node, git_url="https://git.example.com/x")
        db.add(project)
        db.commit()
        db.refresh(project)

        deployer.deploy_sync(db, project, BuildProfile.release)

        assert (workdir / START_SCRIPT_NAME).read_text(encoding="utf-8") != "@echo custom start\n"
    finally:
        db.close()
