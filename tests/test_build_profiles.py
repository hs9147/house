"""빌드 옵션(development/release) 구분이 태그·템플릿·포트·환경에 반영되는지 검증."""
from pathlib import Path

import pytest

from app.models import BuildProfile, Project, ProjectType
from app.services import build as build_service
from app.services.build import (
    PROFILES,
    START_SCRIPT_NAME,
    TEMPLATE_DIR,
    BuildError,
    build_image,
    dockerfile_for,
    install_dependencies,
    internal_port,
    write_start_script,
)


def test_image_tag_suffix():
    sha = "abcdef1234567890"
    dev = PROFILES[BuildProfile.development].image_tag("myapp", sha)
    rel = PROFILES[BuildProfile.release].image_tag("myapp", sha)
    assert dev == "myapp:abcdef123456-dev"
    assert rel == "myapp:abcdef123456"


def test_profile_env_split():
    assert PROFILES[BuildProfile.development].env["NODE_ENV"] == "development"
    assert PROFILES[BuildProfile.release].env["NODE_ENV"] == "production"
    assert PROFILES[BuildProfile.development].resource_factor < 1.0
    assert PROFILES[BuildProfile.release].replicas >= 2


@pytest.mark.parametrize("ptype", [t for t in ProjectType if t != ProjectType.composite])
@pytest.mark.parametrize("profile", list(BuildProfile))
def test_every_type_profile_has_template(ptype, profile, tmp_path):
    df = dockerfile_for(ptype, profile, tmp_path)
    assert df.exists()
    assert df.parent == TEMPLATE_DIR


def test_composite_has_no_toplevel_template(tmp_path):
    """composite는 리포 루트 Dockerfile이 없다 — backend/, frontend/ 서브폴더를
    각각 감지된 타입의 템플릿으로 빌드한다(services/build.py의
    detect_composite_components 참고)."""
    with pytest.raises(FileNotFoundError):
        dockerfile_for(ProjectType.composite, BuildProfile.release, tmp_path)


def test_repo_dockerfile_takes_precedence(tmp_path):
    own = tmp_path / "Dockerfile"
    own.write_text("FROM scratch\n")
    assert dockerfile_for(ProjectType.python, BuildProfile.release, tmp_path) == own


def test_react_release_serves_static_port_80():
    assert internal_port(ProjectType.react, BuildProfile.release) == 80
    assert internal_port(ProjectType.react, BuildProfile.development) == 3000


def test_dev_templates_run_dev_servers():
    react_dev = (TEMPLATE_DIR / "react.development.Dockerfile").read_text(encoding="utf-8")
    python_dev = (TEMPLATE_DIR / "python.development.Dockerfile").read_text(encoding="utf-8")
    python_rel = (TEMPLATE_DIR / "python.release.Dockerfile").read_text(encoding="utf-8")
    assert "npm" in react_dev and "dev" in react_dev
    assert "--reload" in python_dev
    assert "--workers" in python_rel and "--reload" not in python_rel


def test_write_start_script_generates_generic_cmd(tmp_path):
    """windows_service: start.cmd를 조건 없이 자동 생성한다(리포 시그니처로 실행 추정)."""
    path = write_start_script(tmp_path)
    assert path == tmp_path / START_SCRIPT_NAME
    assert path.name == "start.cmd"
    content = path.read_text(encoding="utf-8")
    assert "uvicorn" in content and "npm" in content and "%PORT%" in content


def test_write_start_script_overwrites_unconditionally(tmp_path):
    (tmp_path / START_SCRIPT_NAME).write_text("@echo custom\n", encoding="utf-8")
    write_start_script(tmp_path)
    assert (tmp_path / START_SCRIPT_NAME).read_text(encoding="utf-8") != "@echo custom\n"


def test_html_serves_static_files_port_80():
    assert internal_port(ProjectType.html, BuildProfile.release) == 80
    assert internal_port(ProjectType.html, BuildProfile.development) == 80
    for profile in ("development", "release"):
        content = (TEMPLATE_DIR / f"html.{profile}.Dockerfile").read_text(encoding="utf-8")
        assert "caddy" in content and "file-server" in content


def test_build_image_uses_source_subdir_as_context(monkeypatch, tmp_path):
    """모노레포 서브폴더 프로젝트(예: 콘솔 자기 배포)는 workdir/source_subdir를
    빌드 컨텍스트로 써야 한다 — services/self_deploy.py가 의존하는 동작."""
    (tmp_path / "platform" / "console").mkdir(parents=True)
    project = Project(
        name="paas-console", type=ProjectType.react,
        git_url="https://git.example.com/x", source_subdir="platform/console",
    )

    captured = {}

    class _FakeProc:
        returncode = 0

    def fake_run(cmd, stdout, stderr):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(build_service.subprocess, "run", fake_run)

    result = build_image(project, tmp_path, "a" * 40, BuildProfile.release)

    assert captured["cmd"][-1] == str(tmp_path / "platform" / "console")
    assert result.internal_port == 80  # react release — internal_port(project.type, profile)


def _fake_run_ok(monkeypatch, calls: list):
    class _FakeProc:
        returncode = 0

    def fake_run(cmd, cwd=None, stdout=None, stderr=None):
        calls.append(cmd)
        return _FakeProc()
    monkeypatch.setattr(build_service.subprocess, "run", fake_run)


def test_install_dependencies_runs_npm_ci_when_lockfile_present(monkeypatch, tmp_path):
    """npm은 shutil.which로 실제 경로(Windows의 npm.cmd 포함)를 찾아 그 경로로 실행한다 —
    subprocess가 shell 없이 "npm"을 그대로 넘기면 Windows의 npm.cmd를 못 찾는다."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log")

    assert calls == [["/usr/bin/npm", "ci"]]
    assert "npm ci" in (tmp_path / "env.log").read_text(encoding="utf-8")


def test_install_dependencies_runs_npm_install_without_lockfile(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log")

    assert calls == [["/usr/bin/npm", "install"]]


def test_install_dependencies_raises_clear_error_when_npm_not_on_path(monkeypatch, tmp_path):
    """회귀: npm이 PATH에 있어도(사용자 셸 기준) subprocess가 shell 없이 "npm"을 그대로
    넘기면 Windows에서 npm.cmd를 못 찾아 FileNotFoundError로 실패했다 — "npm 설치·PATH는
    문제없다"는 사용자 보고와 실제 원인(CreateProcess가 PATHEXT를 안 본다)이 어긋나던
    지점. shutil.which가 못 찾으면(진짜로 PATH에 없음) 그 자리에서 분명한 에러를 낸다."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(build_service.shutil, "which", lambda name: None)

    with pytest.raises(BuildError, match="npm 실행 파일을 PATH에서 찾을 수 없습니다"):
        install_dependencies(tmp_path, tmp_path / "env.log")


def test_install_dependencies_creates_venv_and_installs_requirements(monkeypatch, tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    calls: list = []
    _fake_run_ok(monkeypatch, calls)

    install_dependencies(tmp_path, tmp_path / "env.log")

    assert calls[0][:3] == [build_service.sys.executable, "-m", "venv"]  # venv 생성
    assert calls[1][1:4] == ["-m", "pip", "install"]
    assert calls[1][-2:] == ["-r", "requirements.txt"]


def test_install_dependencies_skips_venv_creation_when_it_exists(monkeypatch, tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    calls: list = []
    _fake_run_ok(monkeypatch, calls)

    install_dependencies(tmp_path, tmp_path / "env.log")

    assert len(calls) == 1  # venv 생성 없이 pip install만
    assert calls[0][1:3] == ["-m", "pip"]


def test_install_dependencies_raises_build_error_with_log_on_npm_failure(monkeypatch, tmp_path):
    """실패해도 start.cmd처럼 조용히 다음 줄로 넘어가지 않는다 — 배포가 실패로 남아야 한다."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    class _FakeProc:
        returncode = 1

    monkeypatch.setattr(build_service.subprocess, "run", lambda *a, **kw: _FakeProc())

    log_path = tmp_path / "env.log"
    with pytest.raises(BuildError) as exc:
        install_dependencies(tmp_path, log_path)
    assert exc.value.log_path == log_path


def test_install_dependencies_does_nothing_without_manifest_files(monkeypatch, tmp_path):
    calls: list = []
    _fake_run_ok(monkeypatch, calls)

    install_dependencies(tmp_path, tmp_path / "env.log")

    assert calls == []


def test_streamlit_runs_via_streamlit_cli_port_8501():
    assert internal_port(ProjectType.streamlit, BuildProfile.release) == 8501
    assert internal_port(ProjectType.streamlit, BuildProfile.development) == 8501
    dev = (TEMPLATE_DIR / "streamlit.development.Dockerfile").read_text(encoding="utf-8")
    rel = (TEMPLATE_DIR / "streamlit.release.Dockerfile").read_text(encoding="utf-8")
    assert "streamlit" in dev and "--server.runOnSave=true" in dev
    assert "streamlit" in rel and "--server.runOnSave=true" not in rel
    assert "--server.port=8501" in dev and "--server.port=8501" in rel
