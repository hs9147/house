"""빌드 옵션(development/release) 구분이 태그·템플릿·포트·환경에 반영되는지 검증."""
from pathlib import Path

import pytest

from app.models import BuildProfile, Project, ProjectType
from app.services import build as build_service
from app.services.build import (
    PROFILES,
    START_SCRIPT_NAME,
    TEMPLATE_DIR,
    build_image,
    dockerfile_for,
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


def test_streamlit_runs_via_streamlit_cli_port_8501():
    assert internal_port(ProjectType.streamlit, BuildProfile.release) == 8501
    assert internal_port(ProjectType.streamlit, BuildProfile.development) == 8501
    dev = (TEMPLATE_DIR / "streamlit.development.Dockerfile").read_text(encoding="utf-8")
    rel = (TEMPLATE_DIR / "streamlit.release.Dockerfile").read_text(encoding="utf-8")
    assert "streamlit" in dev and "--server.runOnSave=true" in dev
    assert "streamlit" in rel and "--server.runOnSave=true" not in rel
    assert "--server.port=8501" in dev and "--server.port=8501" in rel
