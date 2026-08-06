"""Build Manager.

빌드 옵션은 development / release 두 프로필로 구분한다 (BuildProfile).

  development: 디버깅 우선 — dev 서버(HMR/--reload), 소스맵, 리소스 절반, 단일 replica,
               이미지 태그에 "-dev" 접미사, {name}-dev.{base_domain} 도메인.
  release:     운영 최적화 — 프로덕션 빌드(minify), 멀티스테이지 이미지, 리소스 전량,
               2차(k8s)에서는 replicas 2 + 롤링 업데이트.

프로젝트 리포에 Dockerfile이 있으면 그것을 우선하고(--build-arg APP_PROFILE 전달),
없으면 templates/dockerfiles/{type}.{profile}.Dockerfile 템플릿을 사용한다.
"""
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings
from ..models import BuildProfile, Project, ProjectType
from .git_auth import auth_args

# git 출력은 로케일이 아니라 UTF-8로 읽는다 — 한글 경로·문서가 섞이면
# 로케일 인코딩(POSIX/cp949)에서 디코딩이 깨져 요청 전체가 실패한다.
_TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "templates" / "dockerfiles"
START_SCRIPT_NAME = "start.cmd"

# windows_service 런타임용 제네릭 시작 스크립트. 타입별 규칙 없이 리포 시그니처
# (package.json / requirements.txt / app.py)로 실행 방법을 런타임에 추정한다.
# PORT/HOST는 windows_service 런타임이 주입한다(HOST=127.0.0.1로 로컬 바인드).
_START_SCRIPT = """@echo off
REM 플랫폼 자동 생성(windows_service) — PORT/HOST는 런타임이 주입한다.
REM 배포 환경 미설정 시 Python/Node 가상환경 및 자동 패키지 설치, 환경 설정 수행
if not defined PORT set PORT=8000
if not defined HOST set HOST=127.0.0.1
set PORT=%PORT%
set HOST=%HOST%

if exist package.json (
  if not exist node_modules (
    echo [PaaS Auto-Provisioning] Node environment missing. Installing dependencies...
    call npm install
  ) else (
    call npm ci --if-present
  )
  call npm run build --if-present
  npm start
) else if exist requirements.txt (
  if not exist .venv (
    echo [PaaS Auto-Provisioning] Python venv missing. Creating .venv...
    py -m venv .venv || python -m venv .venv || python3 -m venv .venv
  )
  if exist .venv\\Scripts\\activate.bat (
    call .venv\\Scripts\\activate.bat
  ) else if exist .venv/bin/activate (
    call .venv/bin/activate
  )
  echo [PaaS Auto-Provisioning] Installing Python dependencies from requirements.txt...
  python -m pip install --upgrade pip --disable-pip-version-check
  python -m pip install --disable-pip-version-check -r requirements.txt
  if exist app\\main.py (
    python -m uvicorn app.main:app --host %HOST% --port %PORT%
  ) else if exist main.py (
    python -m uvicorn main:app --host %HOST% --port %PORT%
  ) else if exist app.py (
    python -m uvicorn app:app --host %HOST% --port %PORT%
  ) else (
    python -m uvicorn app.main:app --host %HOST% --port %PORT%
  )
) else if exist main.py (
  if not exist .venv (
    echo [PaaS Auto-Provisioning] Creating .venv for main.py...
    py -m venv .venv || python -m venv .venv
  )
  if exist .venv\\Scripts\\activate.bat call .venv\\Scripts\\activate.bat
  python -m uvicorn main:app --host %HOST% --port %PORT%
) else if exist app.py (
  if not exist .venv (
    echo [PaaS Auto-Provisioning] Creating .venv for app.py...
    py -m venv .venv || python -m venv .venv
  )
  if exist .venv\\Scripts\\activate.bat call .venv\\Scripts\\activate.bat
  python -m uvicorn app:app --host %HOST% --port %PORT%
) else (
  py -m http.server %PORT% --bind %HOST%
)
"""

# 프로젝트 타입별 컨테이너 내부 포트. (react release는 정적 파일을 caddy로 서빙)
INTERNAL_PORTS: dict[tuple[ProjectType, BuildProfile], int] = {
    (ProjectType.react, BuildProfile.development): 3000,
    (ProjectType.react, BuildProfile.release): 80,
    (ProjectType.node, BuildProfile.development): 3000,
    (ProjectType.node, BuildProfile.release): 3000,
    (ProjectType.python, BuildProfile.development): 8000,
    (ProjectType.python, BuildProfile.release): 8000,
    (ProjectType.llm, BuildProfile.development): 8000,
    (ProjectType.llm, BuildProfile.release): 8000,
    (ProjectType.html, BuildProfile.development): 80,
    (ProjectType.html, BuildProfile.release): 80,
    (ProjectType.streamlit, BuildProfile.development): 8501,
    (ProjectType.streamlit, BuildProfile.release): 8501,
}


@dataclass
class ProfileSpec:
    """프로필이 빌드·배포 전반에 미치는 효과를 한 곳에 모은 정의."""

    profile: BuildProfile
    tag_suffix: str
    env: dict[str, str]
    resource_factor: float  # release 대비 리소스 배율
    replicas: int  # 2차(k8s)에서 사용. 1차는 항상 1.

    def image_tag(self, project_name: str, git_sha: str, component: str | None = None) -> str:
        name = f"{project_name}-{component}" if component else project_name
        return f"{name}:{git_sha[:12]}{self.tag_suffix}"


PROFILES: dict[BuildProfile, ProfileSpec] = {
    BuildProfile.development: ProfileSpec(
        profile=BuildProfile.development,
        tag_suffix="-dev",
        env={"APP_ENV": "development", "NODE_ENV": "development"},
        resource_factor=0.5,
        replicas=1,
    ),
    BuildProfile.release: ProfileSpec(
        profile=BuildProfile.release,
        tag_suffix="",
        env={"APP_ENV": "production", "NODE_ENV": "production"},
        resource_factor=1.0,
        replicas=2,
    ),
}


@dataclass
class BuildResult:
    image_tag: str
    internal_port: int
    log_path: Path
    profile: BuildProfile
    extra_env: dict[str, str] = field(default_factory=dict)


def dockerfile_for(project_type: ProjectType, profile: BuildProfile, workdir: Path) -> Path:
    """리포 자체 Dockerfile 우선, 없으면 타입·프로필별 템플릿."""
    own = workdir / "Dockerfile"
    if own.exists():
        return own
    template = TEMPLATE_DIR / f"{project_type.value}.{profile.value}.Dockerfile"
    if not template.exists():
        raise FileNotFoundError(f"no dockerfile template: {template.name}")
    return template


def write_start_script(workdir: Path) -> Path:
    """windows_service 런타임용 start.cmd를 조건 없이 자동 생성한다(매 배포 시 덮어씀).

    타입별 규칙 없이 리포 시그니처로 실행 방법을 추정하는 제네릭 스크립트라 프로젝트
    타입/프로필을 가리지 않는다 — docker의 dockerfile_for가 이미지 빌드를 담당하는
    자리를 windows_service에서 이 함수가 대신한다."""
    path = workdir / START_SCRIPT_NAME
    path.write_text(_START_SCRIPT, encoding="utf-8")
    return path


def install_dependencies(workdir: Path, log_path: Path) -> None:
    """windows_service 런타임의 명시적 환경설정 단계 — npm/pip install을 배포의 build
    단계로 끝내고, 실패하면 배포 자체를 실패로 남긴다(Docker의 build_image와 대응).

    지금까지는 설치가 start.cmd 안에서 앱 기동과 한 프로세스로 묶여 있었다 — 설치가
    오래 걸리면 헬스체크 타임아웃으로만 보였고("의존성 설치가 헬스 타임아웃을 넘기지
    않는지 확인하세요" 라는 힌트가 남던 이유), 실패해도 배포 상태는 실패로 남지 않았다
    (start.cmd는 설치 실패 후에도 다음 줄로 계속 진행한다). 여기서 헬스체크 창 밖에서
    먼저 끝내면 그 모호함이 없어진다. start.cmd는 계속 조건부로(이미 설치돼 있으면
    빠르게 통과) 같은 설치를 한 번 더 하지만, 이 단계가 실제 게이트다.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        if (workdir / "package.json").exists():
            npm_cmd = ["npm", "ci"] if (workdir / "package-lock.json").exists() else ["npm", "install"]
            log.write(f"[env-setup] {' '.join(npm_cmd)} (cwd={workdir})\n")
            log.flush()
            try:
                proc = subprocess.run(npm_cmd, cwd=workdir, stdout=log, stderr=subprocess.STDOUT)
            except FileNotFoundError as e:
                raise BuildError(f"npm 실행 파일을 찾을 수 없습니다: {e}", log_path) from e
            if proc.returncode != 0:
                raise BuildError(f"npm install 실패 (exit {proc.returncode})", log_path)

        if (workdir / "requirements.txt").exists():
            venv_dir = workdir / ".venv"
            if not venv_dir.exists():
                log.write("[env-setup] python -m venv .venv\n")
                log.flush()
                proc = subprocess.run(
                    [sys.executable, "-m", "venv", str(venv_dir)],
                    cwd=workdir, stdout=log, stderr=subprocess.STDOUT,
                )
                if proc.returncode != 0:
                    raise BuildError(f"venv 생성 실패 (exit {proc.returncode})", log_path)
            venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            log.write("[env-setup] pip install -r requirements.txt\n")
            log.flush()
            try:
                proc = subprocess.run(
                    [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check",
                     "-r", "requirements.txt"],
                    cwd=workdir, stdout=log, stderr=subprocess.STDOUT,
                )
            except FileNotFoundError as e:
                raise BuildError(f"venv python을 찾을 수 없습니다: {e}", log_path) from e
            if proc.returncode != 0:
                raise BuildError(f"pip install 실패 (exit {proc.returncode})", log_path)


def internal_port(project_type: ProjectType, profile: BuildProfile) -> int:
    return INTERNAL_PORTS[(project_type, profile)]


def build_image(
    project: Project,
    workdir: Path,
    git_sha: str,
    profile: BuildProfile,
    *,
    component: str | None = None,
    component_type: ProjectType | None = None,
) -> BuildResult:
    """component가 주어지면(composite 전용) workdir/{component}를 별도 빌드 컨텍스트로
    쓰고, 태그·로그 파일명에 컴포넌트명을 붙여 일반 프로젝트와 충돌하지 않게 한다.

    component가 없고 project.source_subdir가 지정된 경우(모노레포 서브폴더 프로젝트)는
    workdir/{source_subdir}를 빌드 컨텍스트로 쓴다 — 태그·포트 매핑은 일반 프로젝트와 동일."""
    settings = get_settings()
    spec = PROFILES[profile]
    build_type = component_type or project.type
    if component:
        context_dir = workdir / component
    elif project.source_subdir:
        context_dir = workdir / project.source_subdir
    else:
        context_dir = workdir
    tag = spec.image_tag(project.name, git_sha, component=component)
    dockerfile = dockerfile_for(build_type, profile, context_dir)

    log_name = f"{project.name}{f'-{component}' if component else ''}-{git_sha[:12]}{spec.tag_suffix}.log"
    log_path = settings.build_log_dir / log_name
    cmd = [
        "docker", "build",
        "-f", str(dockerfile),
        "-t", tag,
        "--build-arg", f"APP_PROFILE={profile.value}",
        str(context_dir),
    ]
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
        except FileNotFoundError as e:
            raise BuildError(f"[WinError 2] docker CLI 실행 파일을 찾을 수 없습니다: {e}", log_path) from e
    if proc.returncode != 0:
        raise BuildError(f"docker build failed (exit {proc.returncode})", log_path)

    return BuildResult(
        image_tag=tag,
        internal_port=internal_port(build_type, profile),
        log_path=log_path,
        profile=profile,
        extra_env=dict(spec.env),
    )


COMPOSITE_COMPONENTS: tuple[str, str] = ("backend", "frontend")


def detect_composite_components(workdir: Path) -> dict[str, ProjectType] | None:
    """backend/, frontend/ 서브폴더가 둘 다 있어야 composite로 인정한다(하나만 있으면
    일반 단일 프로젝트로 취급 — None 반환). 각 서브폴더의 실제 타입은 시그니처 파일로
    추론하고, 추론 불가면 ValueError로 명확히 실패한다(추측성 기본값 금지)."""
    dirs = {name: workdir / name for name in COMPOSITE_COMPONENTS}
    if not all(d.is_dir() for d in dirs.values()):
        return None
    return {name: _detect_component_type(d) for name, d in dirs.items()}


def detect_project_type(workdir: Path) -> ProjectType | None:
    """리포 루트만 보고 프로젝트 타입을 추론한다(services/gitea_sync.py 전용 — Gitea에서
    기존 리포를 가져올 때 사용자가 type을 지정하지 않으므로). backend/, frontend/가
    둘 다 있으면 composite, 그 외엔 _detect_component_type과 동일한 시그니처 규칙.
    추론 불가하면 None(추측성 기본값 금지 — 호출부가 건너뛰고 보고해야 한다)."""
    if detect_composite_components(workdir) is not None:
        return ProjectType.composite
    try:
        return _detect_component_type(workdir)
    except ValueError:
        return None


def _detect_component_type(component_dir: Path) -> ProjectType:
    if (component_dir / "requirements.txt").exists() or (component_dir / "pyproject.toml").exists():
        return ProjectType.python
    package_json = component_dir / "package.json"
    if package_json.exists():
        try:
            manifest = json.loads(package_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            manifest = {}
        deps = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
        return ProjectType.react if "react" in deps else ProjectType.node
    if (component_dir / "index.html").exists():
        return ProjectType.html
    raise ValueError(
        f"컴포넌트 타입을 추론할 수 없습니다: {component_dir} "
        "(requirements.txt/pyproject.toml, package.json, index.html 중 하나가 필요합니다)"
    )


class BuildError(RuntimeError):
    def __init__(self, message: str, log_path: Path | None = None):
        super().__init__(message)
        self.log_path = log_path


def checkout(project: Project, git_sha: str | None = None) -> tuple[Path, str]:
    """clone 또는 pull 후 (작업 디렉토리, 해석된 커밋 SHA)를 반환한다."""
    settings = get_settings()
    workdir = settings.work_dir / project.name
    if not (workdir / ".git").exists():
        shutil.rmtree(workdir, ignore_errors=True)
        _run_git(["clone", "--branch", project.branch, project.git_url, str(workdir)],
                  git_url=project.git_url)
    else:
        _run_git(["fetch", "origin", project.branch], cwd=workdir, git_url=project.git_url)
        _run_git(["checkout", project.branch], cwd=workdir)
        _run_git(["reset", "--hard", f"origin/{project.branch}"], cwd=workdir)
    if git_sha:
        _run_git(["checkout", git_sha], cwd=workdir)
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workdir, capture_output=True, **_TEXT, check=True
        )
    except FileNotFoundError as e:
        raise BuildError(f"[WinError 2] git 실행 파일을 찾을 수 없습니다: {e}") from e
    return workdir, out.stdout.strip()


def _run_git(args: list[str], cwd: Path | None = None, git_url: str | None = None) -> None:
    auth = auth_args(git_url) if git_url else []
    try:
        proc = subprocess.run(["git", *auth, *args], cwd=cwd, capture_output=True, **_TEXT)
    except FileNotFoundError as e:
        raise BuildError(f"[WinError 2] git 실행 파일을 찾을 수 없습니다: {e}") from e
    if proc.returncode != 0:
        raise BuildError(f"git {args[0]} failed: {proc.stderr.strip()[:500]}")
