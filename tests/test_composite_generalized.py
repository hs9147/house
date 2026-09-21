"""복합 배포 일반화 — 감지된 구조가 곧 배포 명세다(폴더 이름·개수에 묶이지 않는다).

예전 파이프라인은 `backend/`·`frontend/` 두 이름만 다뤘다. 그래서 `api/`+`web/`처럼 이름이
다른 리포나 컴포넌트가 셋인 리포는 감지는 되어도 배포가 실패했다. 여기서는 그 경로를
직접 태운다 — 이 파일의 테스트는 `.env`의 PAAS_RUNTIME_BACKEND와 무관하게 docker 런타임을
못 박는다(windows_service는 단일 start.cmd만 실행해 복합을 지원하지 않으므로, 그 설정이
새면 새 코드가 아예 실행되지 않는다).
"""
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import BuildProfile, Project, ProjectType
from app.services import deployer
from app.services.build import BuildError, BuildResult
from app.services.runtime.base import Endpoint


@pytest.fixture(autouse=True)
def _docker_runtime(monkeypatch, fresh_settings):
    create_app()  # create_all — 이 파일은 TestClient 없이 세션을 직접 연다
    monkeypatch.setenv("PAAS_RUNTIME_BACKEND", "docker")
    monkeypatch.setenv("PAAS_TIER", "small")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeRuntime:
    def __init__(self):
        self.specs = []

    def start(self, spec):
        self.specs.append(spec)
        return Endpoint(host="127.0.0.1", port=9000 + len(self.specs))

    def stop(self, *a): ...
    def status(self, project_name, profile): return "stopped"
    def logs(self, *a, **kw): return ""


def _write(root: Path, rel: str, body: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _setup(db, monkeypatch, tmp_path, name: str):
    project = Project(name=name, type=ProjectType.composite,
                      git_url="https://git.example.com/x")
    db.add(project)
    db.commit()
    db.refresh(project)
    monkeypatch.setattr(deployer, "checkout", lambda p, git_sha=None: (tmp_path, "a" * 40))

    builds: list[dict] = []

    def fake_build(p, workdir, sha, profile, *, component=None, component_type=None,
                   context_subdir=None):
        builds.append({"component": component, "context_subdir": context_subdir,
                       "type": component_type})
        return BuildResult(
            image_tag=f"{p.name}-{component}:{sha[:12]}",
            internal_port=80 if component_type in (ProjectType.react, ProjectType.html) else 8000,
            log_path=Path("/tmp/fake.log"), profile=profile,
        )

    monkeypatch.setattr(deployer, "build_image", fake_build)
    runtime = _FakeRuntime()
    monkeypatch.setattr(deployer, "get_runtime", lambda: runtime)
    routes_seen: list = []
    monkeypatch.setattr(deployer.proxy, "configure_paths",
                        lambda *a, **kw: routes_seen.append(a))
    return project, builds, runtime, routes_seen


def test_deploys_components_whose_folders_are_not_backend_frontend(monkeypatch, tmp_path):
    """회귀: api/·web/은 감지는 되어도 배포가 실패했다."""
    _write(tmp_path, "api/requirements.txt", "fastapi\n")
    _write(tmp_path, "web/package.json", '{"dependencies": {"react": "18"}}')
    db = SessionLocal()
    try:
        project, builds, _runtime, routes_seen = _setup(db, monkeypatch, tmp_path, "shop-a")
        deployer.deploy_composite_sync(db, project, BuildProfile.release)

        assert {b["component"] for b in builds} == {"api", "web"}
        # 화면 타입이 하나뿐이면 그것이 루트를 받는다 — api는 이름을 경로로 쓴다
        routes = {r.path_prefix: r.endpoint.port for r in routes_seen[0][3]}
        assert any(p.endswith("/api/") for p in routes)
        assert any(p.endswith("/shop-a/") or p.endswith("/") for p in routes)
        # 루트 경로가 목록 **뒤**에 와야 앞선 규칙이 가려지지 않는다
        prefixes = [r.path_prefix for r in routes_seen[0][3]]
        assert prefixes == sorted(prefixes, key=len, reverse=True)
    finally:
        db.close()


def test_three_components_all_deploy(monkeypatch, tmp_path):
    """회귀: composite는 정확히 둘일 때만 인정됐다."""
    _write(tmp_path, "api/requirements.txt", "fastapi\n")
    _write(tmp_path, "web/package.json", '{"dependencies": {"react": "18"}}')
    _write(tmp_path, "worker/requirements.txt", "celery\n")
    db = SessionLocal()
    try:
        project, builds, _runtime, routes_seen = _setup(db, monkeypatch, tmp_path, "shop-b")
        deployer.deploy_composite_sync(db, project, BuildProfile.release)
        assert {b["component"] for b in builds} == {"api", "web", "worker"}
        assert len(routes_seen[0][3]) == 3
    finally:
        db.close()


def test_nested_path_keeps_the_tag_name_slash_free(monkeypatch, tmp_path):
    """`apps/web`을 태그에 그대로 쓰면 docker 태그가 유효하지 않다 — 이름과 경로를 나눈다."""
    _write(tmp_path, "services/api/requirements.txt", "fastapi\n")
    _write(tmp_path, "apps/web/package.json", '{"dependencies": {"react": "18"}}')
    db = SessionLocal()
    try:
        project, builds, _runtime, _routes = _setup(db, monkeypatch, tmp_path, "shop-c")
        deployer.deploy_composite_sync(db, project, BuildProfile.release)
        by_component = {b["component"]: b for b in builds}
        assert set(by_component) == {"apps-web", "services-api"}
        assert "/" not in "".join(by_component)  # 태그·유닛 이름에 슬래시가 없다
        # 빌드 컨텍스트는 실제 경로다
        assert by_component["apps-web"]["context_subdir"] == "apps/web"
        assert by_component["services-api"]["context_subdir"] == "services/api"
    finally:
        db.close()


def test_existing_backend_frontend_routes_do_not_move(monkeypatch, tmp_path):
    """이미 배포된 주소다 — backend는 api/, frontend는 루트를 그대로 지킨다."""
    _write(tmp_path, "backend/requirements.txt", "fastapi\n")
    _write(tmp_path, "frontend/package.json", '{"dependencies": {"react": "18"}}')
    db = SessionLocal()
    try:
        project, _builds, _runtime, routes_seen = _setup(db, monkeypatch, tmp_path, "shop-d")
        deployer.deploy_composite_sync(db, project, BuildProfile.release)
        prefixes = [r.path_prefix for r in routes_seen[0][3]]
        assert prefixes[0].endswith("/api/")   # backend
        assert not prefixes[-1].endswith("api/")  # frontend가 루트
    finally:
        db.close()


def test_no_root_component_fails_loudly(monkeypatch, tmp_path):
    """루트를 받을 컴포넌트가 없으면 프로젝트 주소가 404가 된다 — 조용히 배포하지 않는다."""
    _write(tmp_path, "api/requirements.txt", "fastapi\n")
    _write(tmp_path, "worker/requirements.txt", "celery\n")
    db = SessionLocal()
    try:
        project, _builds, _runtime, _routes = _setup(db, monkeypatch, tmp_path, "shop-e")
        with pytest.raises(BuildError, match="루트"):
            deployer.deploy_composite_sync(db, project, BuildProfile.release)
    finally:
        db.close()


def test_two_frontends_cannot_pick_a_root(monkeypatch, tmp_path):
    """화면이 둘이면 어느 것이 대표인지 리포가 말해 주지 않는다 — 임의로 고르지 않는다."""
    _write(tmp_path, "web/package.json", '{"dependencies": {"react": "18"}}')
    _write(tmp_path, "admin/package.json", '{"dependencies": {"react": "18"}}')
    db = SessionLocal()
    try:
        project, _builds, _runtime, _routes = _setup(db, monkeypatch, tmp_path, "shop-f")
        with pytest.raises(BuildError, match="루트"):
            deployer.deploy_composite_sync(db, project, BuildProfile.release)
    finally:
        db.close()


def test_single_component_is_not_a_composite_deploy(monkeypatch, tmp_path):
    """컴포넌트가 하나면 복합 배포가 아니다 — 단일 경로(deploy_sync)로 가야 한다."""
    _write(tmp_path, "requirements.txt", "fastapi\n")
    db = SessionLocal()
    try:
        project, _builds, _runtime, _routes = _setup(db, monkeypatch, tmp_path, "shop-g")
        with pytest.raises(BuildError, match="둘 이상"):
            deployer.deploy_composite_sync(db, project, BuildProfile.release)
    finally:
        db.close()
