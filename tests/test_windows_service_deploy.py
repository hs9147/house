"""windows_service 런타임에서 deploy_sync가 docker build를 건너뛰고, start.cmd를
조건 없이 자동 생성한 뒤 네이티브 런타임으로 기동하는지 검증."""
import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import BuildProfile, DeploymentStatus, Project, ProjectType
from app.services import deployer
from app.services.build import START_SCRIPT_NAME
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
