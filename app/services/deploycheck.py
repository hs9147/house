"""배포 전 점검 — 배포를 걸기 **전에** 알 수 있는 것을 먼저 말한다.

지금까지 이 판단은 전부 사후였다: 배포가 실패하면 진단(services/deploydiag)이 로그와 리포를
보고 원인을 짚었다. 그런데 실패의 절반은 **걸기 전에 이미 정해져 있었다.**

  - supplier-pool: 리포에 의존성 선언이 없었다. 플랫폼은 설치할 것을 찾지 못하고, 앱은
    "streamlit is not installed"로 즉시 죽었다. 화면에는 nssm의 SERVICE_PAUSED만 남았다.
  - 같은 리포: 소스가 `Strategy/`에 있는데 source_subdir가 비어 있었다. 설치·기동이 리포
    루트에서 돌아 아무것도 설치되지 않았다.
  - 기동 스크립트: 저장한 뒤에 검증 규칙이 늘었다(ASCII 전용은 나중에 생겼다). 저장 시점에는
    통과했어도 지금은 아닐 수 있고, 그 사실은 배포가 죽어서야 드러난다.

그래서 **읽기만 하는 점검**을 둔다. LLM을 쓰지 않는다 — 여기서 보는 것은 전부 파일 시스템과
프로젝트 설정에 있는 사실이고, 근거를 파일 이름으로 댈 수 있어야 사람이 바로 고친다.

**막지 않는다.** 배포 버튼을 잠그면 "플랫폼이 틀렸는데 배포도 못 하는" 상황이 생긴다(감지가
못 맞히는 구성은 늘 있다). 점검은 말하고, 배포는 사람이 결정한다.
"""
from pathlib import Path

from ..models import BuildProfile, Project, ProjectType
from . import deploydiag, startscript, structure
from .build import start_script_for
from .workspace import file_tree

# 커밋되면 안 되는 것들. 비밀 파일은 노출이고(실측: supplier-pool의 Strategy/.env), 설치
# 산출물은 리포를 무겁게 하면서 배포의 설치 단계와 어긋난다.
SECRET_FILE_NAMES = (".env", ".env.local", ".env.production", "credentials.json",
                     "id_rsa", "secrets.yaml", "secrets.yml")
INSTALL_ARTIFACT_DIRS = ("node_modules/", ".venv/", "venv/", "__pycache__/")
MAX_FILES = 2000

OK, WARN, FAIL = "ok", "warn", "fail"


def _item(key: str, title: str, status: str, detail: str, fix: str = "") -> dict:
    return {"key": key, "title": title, "status": status, "detail": detail, "fix": fix}


def run(workdir: Path, project: Project,
        profile: BuildProfile = BuildProfile.release) -> dict:
    """점검 결과 — {items, run_dir, summary}. 아무것도 바꾸지 않는다."""
    if not workdir.exists():
        return {
            "run_dir": "",
            "items": [_item("workspace", "리포 워킹카피", FAIL,
                            "리포를 아직 가져오지 못했습니다.",
                            "프로젝트의 git_url·인증을 확인하고 다시 조회하세요.")],
            "summary": {FAIL: 1, WARN: 0, OK: 0},
        }

    sub = project.source_subdir or ""
    run_dir = workdir / sub if sub else workdir
    files = file_tree(workdir, limit=MAX_FILES)
    items = [
        _source_subdir_item(workdir, sub, run_dir),
        _entry_item(run_dir, sub),
        _dependency_item(run_dir, sub),
        _start_script_item(project, profile),
        _streamlit_item(project, run_dir),
        _secret_item(files),
        _artifact_item(files),
    ]
    summary = {OK: 0, WARN: 0, FAIL: 0}
    for it in items:
        summary[it["status"]] += 1
    return {"run_dir": sub or "(리포 루트)", "items": items, "summary": summary}


def _rel_label(sub: str) -> str:
    return sub or "리포 루트"


def _source_subdir_item(workdir: Path, sub: str, run_dir: Path) -> dict:
    """실행 폴더가 실제로 소스가 있는 곳인가 — 아니면 설치가 빈 폴더에서 돈다."""
    if sub and not run_dir.is_dir():
        return _item("source_subdir", "실행 폴더", FAIL,
                     f"지정된 빌드 대상 폴더가 리포에 없습니다: {sub}",
                     "프로젝트의 source_subdir를 리포에 있는 폴더로 바꾸세요.")
    if deploydiag._has_any(run_dir, deploydiag.ENTRY_MARKERS):
        return _item("source_subdir", "실행 폴더", OK,
                     f"{_rel_label(sub)}에서 실행합니다.")
    candidates = deploydiag._candidate_subdirs(workdir)
    if not sub and candidates:
        return _item(
            "source_subdir", "실행 폴더", FAIL,
            f"리포 루트에 시그니처 파일이 없고 하위 폴더에 있습니다: {' · '.join(candidates)}",
            f"source_subdir를 {candidates[0]}로 지정하세요 — 그러지 않으면 설치와 기동이 "
            "루트에서 돌아 아무것도 설치되지 않습니다(실측 사례).")
    return _item("source_subdir", "실행 폴더", WARN,
                 f"{_rel_label(sub)}에 시그니처 파일이 없습니다.",
                 "소스가 있는 폴더를 source_subdir로 지정하거나, 기동 스크립트를 저장하세요.")


def _entry_item(run_dir: Path, sub: str) -> dict:
    markers = deploydiag._has_any(run_dir, deploydiag.ENTRY_MARKERS)
    if markers:
        return _item("entry", "실행 방법 판정", OK,
                     f"{_rel_label(sub)}의 {' · '.join(markers)}로 정합니다.")
    return _item("entry", "실행 방법 판정", FAIL,
                 f"{_rel_label(sub)}에 {' · '.join(deploydiag.ENTRY_MARKERS)} 중 어느 것도 "
                 "없습니다 — 템플릿이 기동 방법을 정할 수 없습니다.",
                 "리포에 해당 파일을 넣거나, 이 리포에 맞는 기동 스크립트를 저장하세요.")


def _dependency_item(run_dir: Path, sub: str) -> dict:
    """**실측에서 가장 비쌌던 항목.** 선언이 없으면 플랫폼은 설치할 근거가 없다."""
    declared = deploydiag._has_any(run_dir, ("requirements.txt", "package.json",
                                             "pyproject.toml"))
    if declared:
        return _item("dependencies", "의존성 선언", OK,
                     f"{_rel_label(sub)}의 {' · '.join(declared)}로 설치합니다.")
    if (run_dir / "index.html").is_file():
        return _item("dependencies", "의존성 선언", OK,
                     "정적 사이트로 보입니다 — 설치할 의존성이 없습니다.")
    return _item(
        "dependencies", "의존성 선언", FAIL,
        f"{_rel_label(sub)}에 requirements.txt·package.json·pyproject.toml이 없습니다 — "
        "플랫폼이 아무것도 설치하지 않습니다.",
        "의존성을 선언하세요. 선언이 없으면 앱은 기동 직후 'module not found'로 죽고, "
        "화면에는 서비스 시작 실패만 남습니다(실측 사례).")


def _start_script_item(project: Project, profile: BuildProfile) -> dict:
    """저장된 스크립트가 **지금** 규칙을 통과하는가 — 규칙은 나중에도 늘어난다."""
    saved = (project.start_scripts or {})
    keys = [k for k in saved if k.startswith(f"{profile.value}:")]
    if not keys:
        return _item("start_script", "기동 스크립트", OK,
                     f"{profile.value}에 지정된 스크립트가 없습니다 — 템플릿으로 뜹니다.")
    problems: list[str] = []
    for key in sorted(keys):
        component = key.split(":", 1)[1]
        found = startscript.validate(start_script_for(project, component, profile))
        problems += [f"{component or '단일'}: {p}" for p in found]
    if problems:
        return _item("start_script", "기동 스크립트", FAIL,
                     "저장된 스크립트가 현재 검증을 통과하지 못합니다 — "
                     + " / ".join(problems[:4]),
                     "기동 스크립트 화면에서 고쳐 저장하세요(저장 시점 이후로 규칙이 "
                     "늘었을 수 있습니다).")
    return _item("start_script", "기동 스크립트", OK,
                 f"저장된 스크립트 {len(keys)}개가 검증을 통과합니다({profile.value}).")


def _streamlit_item(project: Project, run_dir: Path) -> dict:
    if project.type != ProjectType.streamlit:
        return _item("streamlit_entry", "streamlit 진입 파일", OK,
                     "streamlit 프로젝트가 아닙니다.")
    found = [e for e in deploydiag.STREAMLIT_ENTRIES if (run_dir / e).is_file()]
    if found:
        return _item("streamlit_entry", "streamlit 진입 파일", OK,
                     f"{found[0]}로 띄웁니다.")
    return _item("streamlit_entry", "streamlit 진입 파일", WARN,
                 f"{' · '.join(deploydiag.STREAMLIT_ENTRIES)} 중 어느 것도 없습니다.",
                 "진입 파일을 그 이름 중 하나로 두거나, 기동 스크립트에 실행할 파일을 "
                 "직접 적으세요.")


def _secret_item(files: list[str]) -> dict:
    """커밋된 비밀 파일 — 배포 실패는 아니지만 **배포보다 급한 문제**다."""
    hits = [f for f in files if Path(f).name in SECRET_FILE_NAMES]
    if not hits:
        return _item("committed_secrets", "커밋된 비밀 파일", OK,
                     "비밀 파일로 보이는 것이 커밋돼 있지 않습니다.")
    return _item("committed_secrets", "커밋된 비밀 파일", FAIL,
                 f"리포에 커밋돼 있습니다: {' · '.join(hits[:5])}",
                 "값을 회수(rotate)하고 리포에서 지우세요. 플랫폼은 환경변수로 주입합니다 — "
                 "파일을 리포에 두면 소스를 보는 모두가 그 값을 봅니다.")


def _artifact_item(files: list[str]) -> dict:
    hits = sorted({d for d in INSTALL_ARTIFACT_DIRS
                   if any(f.startswith(d) or f"/{d}" in f for f in files)})
    if not hits:
        return _item("install_artifacts", "설치 산출물 커밋", OK,
                     "node_modules·.venv 같은 설치 산출물이 커밋돼 있지 않습니다.")
    return _item("install_artifacts", "설치 산출물 커밋", WARN,
                 f"리포에 들어 있습니다: {' · '.join(hits)}",
                 ".gitignore에 넣고 리포에서 빼세요 — 배포는 설치를 따로 하므로 "
                 "이 파일들은 쓰이지 않고, 체크아웃만 무거워집니다.")


def structure_summary(workdir: Path) -> str:
    """점검 응답에 함께 실을 감지 구조 한 줄(화면이 같은 화면에서 보여 준다)."""
    return structure.summary(structure.detect(workdir)) if workdir.exists() else ""
