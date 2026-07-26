"""복합(백엔드+프론트엔드) 프로젝트 — 자동 감지, 원자적 배포, 부분 실패 시 복구."""
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.main import create_app
from app.models import BuildProfile, Deployment, DeploymentStatus, Project, ProjectType
from app.services import deployer
from app.services.build import BuildResult, detect_composite_components
from app.services.deployer import NoRollbackTarget
from app.services.proxy import PathRoute
from app.services.runtime.base import Endpoint


@pytest.fixture(autouse=True)
def _init_db():
    create_app()  # Base.metadata.create_all(engine) — 이 파일은 TestClient 없이 직접 세션을 연다


# ---- services/build.py: 자동 감지 ----

def test_detect_composite_components_python_backend_react_frontend(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text('{"dependencies": {"react": "^18"}}')

    result = detect_composite_components(tmp_path)
    assert result == {"backend": ProjectType.python, "frontend": ProjectType.react}


def test_detect_returns_none_when_only_one_subfolder(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "requirements.txt").write_text("fastapi\n")
    assert detect_composite_components(tmp_path) is None


def test_detect_html_frontend_node_backend(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "package.json").write_text('{"dependencies": {}}')
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "index.html").write_text("<html></html>")

    result = detect_composite_components(tmp_path)
    assert result == {"backend": ProjectType.node, "frontend": ProjectType.html}


def test_detect_raises_when_type_unrecognizable(tmp_path):
    (tmp_path / "backend").mkdir()  # 시그니처 파일 없음
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "index.html").write_text("<html></html>")
    with pytest.raises(ValueError):
        detect_composite_components(tmp_path)


# ---- services/deployer.py: 원자적 배포 ----

class _FakeRuntime:
    """start()가 호출될 때마다 다른 포트를 배정해, 어느 호출이 어느 endpoint를
    만들었는지 테스트에서 구분할 수 있게 한다."""

    def __init__(self):
        self.calls: list = []

    def start(self, spec):
        self.calls.append(spec)
        port = 9000 + len(self.calls)
        return Endpoint(host="127.0.0.1", port=port)

    def stop(self, *a): ...
    def status(self, *a): return "running"
    def logs(self, *a, **kw): return ""


def _make_project(db, name: str, tmp_path: Path) -> Project:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text('{"dependencies": {"react": "^18"}}')

    project = Project(name=name, type=ProjectType.composite, git_url="https://git.example.com/x")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _mock_checkout(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(deployer, "checkout", lambda project, git_sha=None: (tmp_path, "a" * 40))


def _mock_build_success(monkeypatch):
    def fake_build(project, workdir, sha, profile, *, component=None, component_type=None):
        return BuildResult(
            image_tag=f"{project.name}-{component}:{sha[:12]}",
            internal_port=8000 if component == "backend" else 80,
            log_path=Path("/tmp/fake.log"), profile=profile,
        )
    monkeypatch.setattr(deployer, "build_image", fake_build)


def test_composite_deploy_both_succeed_configures_proxy_once(monkeypatch, tmp_path):
    db = SessionLocal()
    try:
        project = _make_project(db, "shop", tmp_path)
        _mock_checkout(monkeypatch, tmp_path)
        _mock_build_success(monkeypatch)
        runtime = _FakeRuntime()
        monkeypatch.setattr(deployer, "get_runtime", lambda: runtime)
        proxy_calls = []
        monkeypatch.setattr(
            deployer.proxy, "configure_paths",
            lambda *a, **kw: proxy_calls.append((a, kw)),
        )

        records = deployer.deploy_composite_sync(db, project, BuildProfile.release)

        assert set(records) == {"backend", "frontend"}
        assert records["backend"].status == DeploymentStatus.running
        assert records["frontend"].status == DeploymentStatus.running
        assert records["backend"].deploy_group_id == records["frontend"].deploy_group_id
        assert len(proxy_calls) == 1
        routes: list[PathRoute] = proxy_calls[0][0][3]
        # organization_id 없는 프로젝트라 조직 자리는 "_" — path_prefix_for("_"/{name}/[api/])
        assert [r.path_prefix for r in routes] == ["/apps/_/shop/api/", "/apps/_/shop/"]
        assert len(runtime.calls) == 2  # backend, frontend 각각 한 번씩 start()
    finally:
        db.close()


def test_composite_deploy_frontend_fails_backend_restored_from_previous(monkeypatch, tmp_path):
    """이전에 성공 배포가 있는 상태에서 frontend 빌드가 실패하면, backend는 새 버전으로
    가되 frontend는 직전 정상 이미지로 복구되고 프록시는 정확히 한 번만 갱신된다 —
    부분 실패가 서비스 중단으로 이어지지 않는다."""
    db = SessionLocal()
    try:
        project = _make_project(db, "shop2", tmp_path)

        # 직전 성공 배포 그룹을 미리 만들어 둔다(복구 대상).
        prev_backend = Deployment(
            project_id=project.id, git_sha="b" * 40, image_tag="shop2-backend:prev",
            profile=BuildProfile.release, status=DeploymentStatus.running,
            component="backend", deploy_group_id="prev-group", internal_port=8000,
        )
        prev_frontend = Deployment(
            project_id=project.id, git_sha="b" * 40, image_tag="shop2-frontend:prev",
            profile=BuildProfile.release, status=DeploymentStatus.running,
            component="frontend", deploy_group_id="prev-group", internal_port=80,
        )
        db.add_all([prev_backend, prev_frontend])
        db.commit()

        _mock_checkout(monkeypatch, tmp_path)

        def fake_build(project, workdir, sha, profile, *, component=None, component_type=None):
            if component == "frontend":
                from app.services.build import BuildError
                raise BuildError("npm build failed")
            return BuildResult(
                image_tag=f"{project.name}-{component}:{sha[:12]}",
                internal_port=8000, log_path=Path("/tmp/fake.log"), profile=profile,
            )
        monkeypatch.setattr(deployer, "build_image", fake_build)

        runtime = _FakeRuntime()
        monkeypatch.setattr(deployer, "get_runtime", lambda: runtime)
        proxy_calls = []
        monkeypatch.setattr(
            deployer.proxy, "configure_paths",
            lambda *a, **kw: proxy_calls.append((a, kw)),
        )

        with pytest.raises(Exception, match="npm build failed"):
            deployer.deploy_composite_sync(db, project, BuildProfile.release)

        # 프록시는 정확히 한 번, 두 endpoint가 모두 확보된 뒤에만 갱신됐다.
        assert len(proxy_calls) == 1
        routes: list[PathRoute] = proxy_calls[0][0][3]
        ports = {r.path_prefix: r.endpoint.port for r in routes}
        assert ports["/apps/_/shop2/api/"] != ports["/apps/_/shop2/"]  # 서로 다른 컨테이너

        # 새 backend 배포 행은 running, 이번 시도의 frontend 행은 failed로 기록.
        rows = db.execute(
            select(Deployment).where(Deployment.project_id == project.id)
            .order_by(Deployment.id)
        ).scalars().all()
        this_attempt = [r for r in rows if r.deploy_group_id not in (None, "prev-group")]
        backend_new = next(r for r in this_attempt if r.component == "backend")
        frontend_new = next(r for r in this_attempt if r.component == "frontend")
        assert backend_new.status == DeploymentStatus.running
        assert frontend_new.status == DeploymentStatus.failed
        assert "npm build failed" in frontend_new.error

        # 복구 대상이었던 이전 frontend 행은 running으로 유지(재시작됨).
        db.refresh(prev_frontend)
        assert prev_frontend.status == DeploymentStatus.running
        # 이전 backend 행은 새 backend가 떴으므로 stopped로 전환.
        db.refresh(prev_backend)
        assert prev_backend.status == DeploymentStatus.stopped
    finally:
        db.close()


def test_composite_deploy_first_ever_partial_failure_no_rollback_target(monkeypatch, tmp_path):
    """이전 배포가 전혀 없는 첫 composite 배포에서 한쪽이 실패하면(되돌릴 버전 없음),
    성공한 쪽도 failed로 기록되고(무한 building 방지) 프록시는 전혀 건드리지 않는다."""
    db = SessionLocal()
    try:
        project = _make_project(db, "shop3", tmp_path)
        _mock_checkout(monkeypatch, tmp_path)

        def fake_build(project, workdir, sha, profile, *, component=None, component_type=None):
            if component == "frontend":
                from app.services.build import BuildError
                raise BuildError("no frontend deps")
            return BuildResult(
                image_tag=f"{project.name}-{component}:{sha[:12]}",
                internal_port=8000, log_path=Path("/tmp/fake.log"), profile=profile,
            )
        monkeypatch.setattr(deployer, "build_image", fake_build)
        monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
        proxy_calls = []
        monkeypatch.setattr(
            deployer.proxy, "configure_paths",
            lambda *a, **kw: proxy_calls.append((a, kw)),
        )

        with pytest.raises(Exception, match="no frontend deps"):
            deployer.deploy_composite_sync(db, project, BuildProfile.release)

        assert proxy_calls == []
        rows = db.execute(
            select(Deployment).where(Deployment.project_id == project.id)
        ).scalars().all()
        assert len(rows) == 2
        assert all(r.status == DeploymentStatus.failed for r in rows)
    finally:
        db.close()


def test_rollback_composite_no_target_raises(tmp_path):
    db = SessionLocal()
    try:
        project = _make_project(db, "shop4", tmp_path)
        with pytest.raises(NoRollbackTarget):
            deployer.rollback_composite(db, project, BuildProfile.release)
    finally:
        db.close()
