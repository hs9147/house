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
    docker_build_log_path,
    dockerfile_for,
    env_setup_log_path,
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


def test_write_start_script_falls_back_to_vite_preview_when_no_start_script(tmp_path):
    """npm create vite@latest로 만든 프로젝트의 package.json에는 "start" 스크립트가
    없다(dev/build/preview만 있음) — 무조건 "npm start"를 부르면 "Missing script:
    start"로 즉시 죽는다. vite가 설치돼 있으면(devDependency라 npm ci/install이
    항상 함께 설치) 이미 빌드된 산출물을 "vite preview"로 서빙해야 한다.

    (회귀 방지: _START_SCRIPT는 raw 문자열이 아니라서 "\\v"·"\\a" 같은 시퀀스가
    Python 이스케이프로 해석돼 "\\vite.cmd"가 "ite.cmd"로 깨지는 식의 실수가
    나기 쉽다 — 정확한 경로 문자열을 그대로 검증한다.)"""
    content = write_start_script(tmp_path).read_text(encoding="utf-8")
    assert r"node_modules\.bin\vite.cmd preview --config paas-preview.config.mjs --host %HOST% --port %PORT%" in content
    assert r"exist node_modules\.bin\vite.cmd" in content


def test_start_script_does_not_test_errorlevel_inside_the_block(tmp_path):
    """분기 판정에 %errorlevel% 치환을 쓰면 vite 분기가 영영 실행되지 않는다.

    이 검사는 "if exist package.json (" 로 열린 괄호 블록 안에 있고, 배치의 괄호
    블록은 통째로 한 번 파싱되면서 %errorlevel%이 **블록에 들어오기 전** 값으로
    치환된다. 바로 위가 set이라 그 값은 항상 0 — 즉 "if 0==0"이 되어 package.json에
    start가 있든 없든 늘 npm start로 갔고, Vite 프로젝트는 "Missing script: start"로
    죽었다. 실행 시점에 평가되는 "if errorlevel"이어야 한다.
    """
    content = write_start_script(tmp_path).read_text(encoding="utf-8")
    body = content[content.index("if exist package.json ("):content.index(") else if exist requirements.txt")]
    # 주석(REM)은 실행되지 않으므로 제외한다 — 위 설명 자체가 그 토큰을 담고 있다.
    executed = "\n".join(ln for ln in body.splitlines() if not ln.strip().upper().startswith("REM"))
    assert "%errorlevel%" not in executed, "괄호 블록 안에서는 %errorlevel% 치환이 통하지 않는다"
    assert "if errorlevel 1 (" in executed


def test_start_script_detects_start_script_with_node_not_text_search(tmp_path):
    """텍스트 검색은 "start:dev"·"pre-start" 같은 다른 이름에도 걸려, start가 없는
    프로젝트를 있다고 오판한다 — package.json의 scripts.start를 직접 본다."""
    content = write_start_script(tmp_path).read_text(encoding="utf-8")
    assert "p.scripts?p.scripts.start:0" in content
    assert "findstr" not in content
    # 배치 블록 안에서 &&·|| 는 따옴표 밖으로 새면 블록을 깨뜨린다 — 아예 쓰지 않는다.
    node_line = next(ln for ln in content.splitlines() if ln.strip().startswith("node -e"))
    assert "&&" not in node_line and "||" not in node_line


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

    def fake_run(cmd, stdout, stderr, timeout=None):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(build_service.subprocess, "run", fake_run)

    result = build_image(project, tmp_path, "a" * 40, BuildProfile.release)

    assert captured["cmd"][-1] == str(tmp_path / "platform" / "console")
    assert result.internal_port == 80  # react release — internal_port(project.type, profile)


def _fake_run_ok(monkeypatch, calls: list):
    class _FakeProc:
        returncode = 0

    def fake_run(cmd, cwd=None, stdout=None, stderr=None, timeout=None):
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

    assert calls == [["/usr/bin/npm", "ci", "--include=dev"],
                     ["/usr/bin/npm", "run", "build", "--if-present"]]
    assert "npm ci" in (tmp_path / "env.log").read_text(encoding="utf-8")


def test_install_dependencies_runs_npm_install_without_lockfile(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log")

    assert calls == [["/usr/bin/npm", "install", "--include=dev"],
                     ["/usr/bin/npm", "run", "build", "--if-present"]]


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


def test_log_path_helpers_are_deterministic_before_the_build_starts():
    """deployer.py는 실제 빌드/설치를 부르기 전에 이 값을 Deployment.build_log_path에
    커밋해야 한다 — git_sha/profile(/component)만으로 값이 정해지므로 그게 가능하다."""
    sha = "abcdef1234567890"
    assert env_setup_log_path("chatbot", sha, BuildProfile.release).name == "chatbot-abcdef123456-env.log"
    assert env_setup_log_path("chatbot", sha, BuildProfile.development).name == "chatbot-abcdef123456-dev-env.log"
    assert docker_build_log_path("myapp", sha, BuildProfile.release).name == "myapp-abcdef123456.log"
    assert docker_build_log_path("myapp", sha, BuildProfile.release, "backend").name == "myapp-backend-abcdef123456.log"


def test_install_dependencies_raises_clear_error_on_npm_timeout(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, cwd=None, stdout=None, stderr=None, timeout=None):
        raise build_service.subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(build_service.subprocess, "run", fake_run)

    log_path = tmp_path / "env.log"
    with pytest.raises(BuildError, match="npm install이 .*초 내에 끝나지 않아") as exc:
        install_dependencies(tmp_path, log_path)
    assert exc.value.log_path == log_path


def test_install_dependencies_raises_clear_error_on_venv_timeout(monkeypatch, tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")

    def fake_run(cmd, cwd=None, stdout=None, stderr=None, timeout=None):
        raise build_service.subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(build_service.subprocess, "run", fake_run)

    with pytest.raises(BuildError, match="venv 생성이 .*초 내에 끝나지 않아"):
        install_dependencies(tmp_path, tmp_path / "env.log")


def test_install_dependencies_raises_clear_error_on_pip_timeout(monkeypatch, tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()  # venv 생성을 건너뛰어 pip install 호출만 남긴다

    def fake_run(cmd, cwd=None, stdout=None, stderr=None, timeout=None):
        raise build_service.subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(build_service.subprocess, "run", fake_run)

    with pytest.raises(BuildError, match="pip install이 .*초 내에 끝나지 않아"):
        install_dependencies(tmp_path, tmp_path / "env.log")


def test_build_image_raises_clear_error_on_docker_timeout(monkeypatch, tmp_path):
    project = Project(name="timeoutapp", type=ProjectType.python, git_url="https://git.example.com/x")

    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        raise build_service.subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(build_service.subprocess, "run", fake_run)

    with pytest.raises(BuildError, match="docker build가 .*초 내에 끝나지 않아") as exc:
        build_image(project, tmp_path, "a" * 40, BuildProfile.release)
    assert exc.value.log_path == docker_build_log_path(
        project.name, "a" * 40, BuildProfile.release,
    )


def test_preview_config_written_only_for_node_projects(tmp_path):
    """vite preview 분기가 --config로 이 파일을 참조한다. 파이썬 프로젝트 작업
    디렉터리에는 남기지 않는다."""
    from app.services.build import PREVIEW_CONFIG_NAME

    write_start_script(tmp_path)
    assert not (tmp_path / PREVIEW_CONFIG_NAME).exists()

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    write_start_script(tmp_path)
    assert (tmp_path / PREVIEW_CONFIG_NAME).exists()


def test_preview_config_keeps_project_config_and_allows_proxy_host(tmp_path):
    """allowedHosts만 얹고 프로젝트 설정은 살려야 한다 — 통째로 대체하면 base·outDir가
    사라져 서브패스 배포가 깨진다. 그리고 start.cmd가 실제로 이 파일을 참조해야 한다."""
    from app.services.build import PREVIEW_CONFIG_NAME

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    content = write_start_script(tmp_path).read_text(encoding="utf-8")
    config = (tmp_path / PREVIEW_CONFIG_NAME).read_text(encoding="utf-8")

    assert f"--config {PREVIEW_CONFIG_NAME}" in content
    assert "loadConfigFromFile" in config and "mergeConfig" in config
    assert "allowedHosts: true" in config
    # vite가 기본으로 찾는 이름이면 loadConfigFromFile이 자기 자신을 다시 읽는다.
    assert not PREVIEW_CONFIG_NAME.startswith("vite.config")


def test_start_script_does_not_reinstall_or_rebuild(tmp_path):
    """start.cmd는 서비스가 뜰 때마다 실행된다 — 재부팅·SW 업데이트·크래시 재시작 포함.
    여기에 설치·빌드를 두면 그때마다 반복되고, npm ci는 node_modules를 통째로 지우고
    다시 설치하므로 "이미 있으면 빠르게 통과"도 아니다. 배포 한 번에 npm.cmd가 세 번
    뜨던 원인이라, 설치·빌드는 build 단계(install_dependencies)에만 둔다.
    """
    content = write_start_script(tmp_path).read_text(encoding="utf-8")
    executed = "\n".join(
        ln for ln in content.splitlines() if not ln.strip().upper().startswith("REM")
    )
    assert "npm ci" not in executed
    assert "npm run build" not in executed
    # node_modules가 아예 없을 때의 보루만 남는다(그때도 ci가 아니라 install).
    assert "if not exist node_modules (" in executed
    assert executed.count("call npm install") == 1


def test_install_dependencies_builds_after_installing(monkeypatch, tmp_path):
    """빌드가 build 단계에 있어야 실패가 배포 실패로 드러난다 — start.cmd에 있으면
    call로 실행돼 실패해도 다음 줄로 넘어가고 배포는 성공으로 남는다."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log")

    assert calls[-1] == ["/usr/bin/npm", "run", "build", "--if-present"]
    assert "npm run build" in (tmp_path / "env.log").read_text(encoding="utf-8")


def test_install_forces_dev_dependencies(monkeypatch, tmp_path):
    """vite·tsc 같은 빌드 도구는 devDependencies에 있다. 환경에 NODE_ENV=production이
    있거나 .npmrc에 omit=dev가 있으면 npm이 그걸 통째로 건너뛰어 node_modules/.bin
    자체가 안 생기고, 빌드가 "'vite' is not recognized"로 죽는다. 이 설치는 paas
    서비스 프로세스의 환경을 상속하므로 서버 설정 하나에 배포가 끌려다닌다 — CLI
    플래그가 환경변수·.npmrc보다 우선하므로 여기서 못박는다.
    """
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log")

    assert "--include=dev" in calls[0]


def test_start_script_explains_missing_vite(tmp_path):
    """vite.cmd를 못 찾으면 조용히 npm start로 넘어가 "Missing script: start"만 남는다 —
    로그만 봐서는 devDependencies가 빠졌다는 진짜 원인을 알 수 없다."""
    content = write_start_script(tmp_path).read_text(encoding="utf-8")
    assert "vite.cmd is missing" in content
    assert "NODE_ENV=production" in content


def _make_vite(tmp_path, build="vite build"):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"build": "%s"}}' % build, encoding="utf-8")


def test_build_receives_public_subpath_for_vite(monkeypatch, tmp_path):
    """프록시가 서브패스 접두어를 벗기고 넘기므로 앱은 "/"를 받지만, 브라우저가 보는
    주소는 /apps/조직/프로젝트/[dev/]다. 기본값(base="/")으로 빌드하면 HTML이 자산을
    /assets/...로 참조하고 그 요청은 어떤 라우팅에도 안 걸려 404가 된다 — 제목만 뜨는
    빈 화면이 되던 자리다."""
    _make_vite(tmp_path)
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log", base_path="/apps/org/shop/dev/")

    assert calls[-1] == [
        "/usr/bin/npm", "run", "build", "--if-present", "--", "--base=/apps/org/shop/dev/",
    ]


def test_build_does_not_pass_base_to_non_vite_projects(monkeypatch, tmp_path):
    """--base는 Vite 옵션이다 — webpack/next 등에 넘기면 모르는 인자로 빌드가 깨진다.

    .bin/vite 존재로 판별하면 안 된다 — npm은 전이 의존의 bin도 최상위 .bin에
    호이스팅하므로, vite를 간접적으로만 끌고 오는 Next 프로젝트도 통과해 버린다.
    """
    (tmp_path / "package.json").write_text('{"scripts": {"build": "next build"}}', encoding="utf-8")
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "vite").write_text("", encoding="utf-8")  # 전이 의존으로 호이스팅된 상태
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log", base_path="/apps/org/shop/dev/")

    assert calls[-1] == ["/usr/bin/npm", "run", "build", "--if-present"]


def test_build_without_base_path_is_unchanged(monkeypatch, tmp_path):
    _make_vite(tmp_path)
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log")

    assert calls[-1] == ["/usr/bin/npm", "run", "build", "--if-present"]


def _npm_calls(calls):
    return [c for c in calls if c[0].endswith("npm")]


def test_install_is_skipped_when_dependencies_unchanged(monkeypatch, tmp_path):
    """npm ci는 node_modules를 통째로 지우고 다시 설치한다. 의존성이 안 바뀌었는데도
    배포마다 반복하면 시간이 그대로 나가고 서비스 재기동까지 늦어진다."""
    (tmp_path / "package.json").write_text('{"name":"a"}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{"v":1}', encoding="utf-8")
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log")
    assert _npm_calls(calls)[0][:2] == ["/usr/bin/npm", "ci"]

    calls.clear()
    install_dependencies(tmp_path, tmp_path / "env.log")
    assert not any(c[1] in ("ci", "install") for c in _npm_calls(calls)), "설치를 또 했다"
    assert "설치를 건너뜁니다" in (tmp_path / "env.log").read_text(encoding="utf-8")
    # 빌드는 매번 돌아야 한다 — 소스는 배포마다 바뀐다.
    assert _npm_calls(calls)[-1][1:3] == ["run", "build"]


def test_install_runs_again_when_lockfile_changes(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text('{"name":"a"}', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{"v":1}', encoding="utf-8")
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")
    install_dependencies(tmp_path, tmp_path / "env.log")

    (tmp_path / "package-lock.json").write_text('{"v":2}', encoding="utf-8")
    calls.clear()
    install_dependencies(tmp_path, tmp_path / "env.log")
    assert _npm_calls(calls)[0][:2] == ["/usr/bin/npm", "ci"]


def test_failed_install_is_not_remembered_as_current(monkeypatch, tmp_path):
    """실패한 설치를 "최신"으로 기억하면 다음 배포가 깨진 node_modules로 그냥 진행한다."""
    (tmp_path / "package.json").write_text('{"name":"a"}', encoding="utf-8")
    (tmp_path / "node_modules").mkdir()

    class _Fail:
        returncode = 1

    monkeypatch.setattr(build_service.subprocess, "run", lambda *a, **kw: _Fail())
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(build_service.BuildError):
        install_dependencies(tmp_path, tmp_path / "env.log")
    assert not (tmp_path / build_service.INSTALL_STAMP).exists()


def test_base_is_not_passed_when_vite_is_not_the_last_command(monkeypatch, tmp_path):
    """npm은 인자를 스크립트 **끝**에 이어 붙인다 — vite가 마지막이 아니면 --base가
    엉뚱한 명령으로 간다."""
    _make_vite(tmp_path, build="vite build && node scripts/post.js")
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log", base_path="/apps/org/shop/dev/")

    assert calls[-1] == ["/usr/bin/npm", "run", "build", "--if-present"]


def test_base_is_passed_when_vite_runs_after_tsc(monkeypatch, tmp_path):
    """Vite 스캐폴드의 흔한 형태 — "tsc -b && vite build"는 --base가 vite로 간다."""
    _make_vite(tmp_path, build="tsc -b && vite build")
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log", base_path="/apps/org/shop/dev/")

    assert calls[-1][-1] == "--base=/apps/org/shop/dev/"


def test_dev_profile_skips_the_build(monkeypatch, tmp_path):
    """dev 서버가 소스를 즉석에서 변환해 서빙하므로 빌드 산출물을 쓰지 않는다 —
    배포마다 도는 빌드가 그대로 낭비다."""
    _make_vite(tmp_path)
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log", base_path="/apps/o/s/dev/", build=False)

    assert not any(c[1:3] == ["run", "build"] for c in calls if len(c) > 2)
    assert "빌드를 건너뜁니다" in (tmp_path / "env.log").read_text(encoding="utf-8")


def test_dev_profile_still_installs_python_dependencies(monkeypatch, tmp_path):
    """빌드를 건너뛴다고 그 아래 pip 설치까지 건너뛰면 안 된다 — 두 파일을 다 가진
    프로젝트에서 파이썬 의존성이 통째로 빠진다."""
    _make_vite(tmp_path)
    (tmp_path / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    calls: list = []
    _fake_run_ok(monkeypatch, calls)
    monkeypatch.setattr(build_service.shutil, "which", lambda name: f"/usr/bin/{name}")

    install_dependencies(tmp_path, tmp_path / "env.log", build=False)

    assert any("pip" in " ".join(map(str, c)) for c in calls), calls


def test_start_script_runs_dev_server_for_development_profile(tmp_path):
    """dev 프로필은 빌드본이 아니라 dev 서버로 띄운다. 프록시가 서브패스를 벗기지 않고
    넘기므로 dev 서버에도 같은 base를 줘야 /@vite/client 요청이 아귀가 맞는다."""
    content = write_start_script(tmp_path).read_text(encoding="utf-8")
    assert '"%PAAS_PROFILE%"=="development"' in content
    assert "--base %PAAS_BASE_PATH%" in content
    assert "call npm run dev" in content
