"""프로젝트 구조 감지 — 리포의 파일 구조에서 **배포 단위**를 읽는다(결정론, LLM 없음).

**왜 타입 하나로는 부족한가.** ProjectType은 단일 enum이고 그것이 결정하는 것은 두
가지다: Dockerfile 템플릿(`templates/{type}.{profile}.Dockerfile`)과 컨테이너 내부
포트(build.INTERNAL_PORTS). 그래서 한 리포에 백엔드와 프론트엔드가 함께 있으면 그 둘은
**서로 다른 템플릿·다른 포트**로 빌드돼야 하는데, 값 하나로는 표현할 수 없다. 예전에는
`composite`라는 enum 값을 하나 더 두고 배포할 때마다 `backend/`·`frontend/` 폴더 이름을
다시 확인했다 — 이름이 `api/`·`web/`이거나 컴포넌트가 셋이면 잡히지 않았고, 구성 내용이
어디에도 저장되지 않아 사람이 확인할 수도 없었다.

**그래서 구조가 곧 배포 명세다.** 감지 결과는 컴포넌트 목록이고, 각 항목이 빌드 한 번에
대응한다. 단일 프로젝트도 path="." 하나짜리 구조라서 특수 경우가 아니다.

**왜 결정론인가.** 시그니처 파일(requirements.txt·package.json·index.html …)의 존재는
추정이 아니라 사실이다. 판정하지 못한 폴더는 type=None으로 남겨 화면에 드러낸다 —
추측성 기본값을 넣으면 엉뚱한 Dockerfile로 빌드되고, 그건 조용히 실패하는 쪽이다.
(codemap.py·ontology.py와 같은 판단.)
"""
import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import ProjectType, utcnow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# 이 파일이 있으면 그 폴더는 해당 타입의 배포 단위다. 위에서부터 먼저 맞는 것을 쓴다 —
# streamlit 앱에도 requirements.txt가 있으므로 더 구체적인 신호를 먼저 본다.
#
# 판정에 쓰는 것은 **빌드 방식이 갈리는 신호**뿐이다. 프레임워크 이름을 더 잘게 나누는
# 것(Next.js·Vue·NestJS 등)은 workspace.detect_project_stack_and_deps가 LLM 컨텍스트용으로
# 이미 하고 있고, 여기서 그걸 늘려도 Dockerfile이 갈리지 않으면 배포에 쓸모가 없다.
MARKER_FILES = ("requirements.txt", "pyproject.toml", "package.json", "index.html")

# 서브폴더를 얼마나 깊이 들어가 볼지. 1이면 리포 루트의 바로 아래만 본다.
# 2로 둔 이유: `services/api`, `apps/web`처럼 한 단계 묶는 모노레포가 흔하다.
MAX_DEPTH = 2

# 감지에서 제외할 폴더 이름 — 여기 안의 package.json·requirements.txt는 그 프로젝트의
# 배포 단위가 아니라 남의 코드이거나 빌드 산출물이다. node_modules를 빼지 않으면 의존성
# 하나하나가 컴포넌트로 잡힌다.
SKIP_DIRS = frozenset({
    ".git", ".github", ".venv", "venv", "env", "node_modules", "__pycache__",
    "dist", "build", "out", ".next", ".nuxt", "target", "vendor",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
    "docs", "migrations", "templates", "infra",
})

# 한 리포에서 인정할 컴포넌트 상한. 모노레포 하나가 배포 명세를 통째로 채우는 것을 막는다.
MAX_COMPONENTS = 12


def detect(workdir: Path, max_depth: int = MAX_DEPTH) -> dict:
    """리포 구조 → 배포 명세.

    반환 형태:
      {"method": "deterministic",
       "components": [{"name", "path", "type"}, ...]}

    - path는 리포 루트 기준 상대 경로("."은 루트 자체).
    - type은 감지 실패 시 None — 호출부가 "판정 불가"로 드러내야 한다.
    - 루트에 시그니처가 있고 서브폴더에도 있으면 **둘 다** 싣는다. 루트의 것이 랩퍼
      스크립트일 수도, 진짜 앱일 수도 있어서 여기서 하나를 골라 버리면 판단 근거가
      사라진다 — 고르는 것은 사람이나 배포 설정의 일이다.
    """
    components: list[dict] = []
    if not workdir.is_dir():
        return {"method": "deterministic", "components": components}

    if _has_marker(workdir):
        components.append(_component(".", workdir))
    for path in _candidate_dirs(workdir, max_depth):
        if len(components) >= MAX_COMPONENTS:
            break
        components.append(_component(path.relative_to(workdir).as_posix(), path))
    return {"method": "deterministic", "components": components}


def _candidate_dirs(root: Path, max_depth: int) -> list[Path]:
    """시그니처 파일을 가진 서브폴더(정렬된 순서). 이름은 보지 않는다.

    이름 목록(backend/frontend/api/web…)으로 찾으면 그 목록에 없는 리포는 영원히 잡히지
    않는다. 파일이 있다는 사실만 본다.
    """
    found: list[Path] = []

    def walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(p for p in current.iterdir() if p.is_dir())
        except OSError:
            return
        for entry in entries:
            if entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            if _has_marker(entry):
                found.append(entry)
                # 컴포넌트 안쪽은 더 파지 않는다 — 그 안의 package.json은 그 컴포넌트의
                # 일부다(예: frontend/functions).
                continue
            walk(entry, depth + 1)

    walk(root, 1)
    return found


def _has_marker(directory: Path) -> bool:
    return any((directory / name).is_file() for name in MARKER_FILES)


def _component(rel_path: str, directory: Path) -> dict:
    return {
        # 이름은 경로에서 만든다. 루트는 "app"이라고 부른다 — 화면 배지에 "."은 읽히지 않는다.
        "name": "app" if rel_path == "." else rel_path.replace("/", "-"),
        "path": rel_path,
        "type": (t.value if (t := detect_type(directory)) else None),
    }


def detect_type(directory: Path) -> ProjectType | None:
    """폴더 하나의 타입. 판정하지 못하면 None(추측성 기본값 금지)."""
    if _is_streamlit(directory):
        return ProjectType.streamlit
    if _is_llm(directory):
        return ProjectType.llm
    if (directory / "requirements.txt").is_file() or (directory / "pyproject.toml").is_file():
        return ProjectType.python
    package_json = directory / "package.json"
    if package_json.is_file():
        deps = _node_deps(package_json)
        return ProjectType.react if "react" in deps else ProjectType.node
    if (directory / "index.html").is_file():
        return ProjectType.html
    return None


def _requirement_names(directory: Path) -> set[str]:
    """requirements.txt·pyproject.toml에 적힌 패키지 이름(소문자).

    버전·환경 마커는 떼고 이름만 본다 — `streamlit==1.2.3`도 `streamlit`으로 읽혀야 한다.
    """
    text = ""
    for name in ("requirements.txt", "pyproject.toml"):
        path = directory / name
        if path.is_file():
            try:
                text += path.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                pass
    names: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().strip('",\'')
        for sep in ("==", ">=", "<=", "~=", ">", "<", "[", ";", " @ "):
            line = line.split(sep, 1)[0].strip()
        if line:
            names.add(line)
    return names


def _is_streamlit(directory: Path) -> bool:
    return "streamlit" in _requirement_names(directory)


# vLLM은 GPU 배정·모델 로딩이 따라붙는 별개 배포 형상이라 python과 나눠야 한다.
_LLM_PACKAGES = frozenset({"vllm", "text-generation", "text-generation-inference"})


def _is_llm(directory: Path) -> bool:
    return bool(_LLM_PACKAGES & _requirement_names(directory))


def _node_deps(package_json: Path) -> set[str]:
    try:
        manifest = json.loads(package_json.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(manifest, dict):
        return set()
    deps: dict = {}
    for key in ("dependencies", "devDependencies"):
        value = manifest.get(key)
        if isinstance(value, dict):
            deps.update(value)
    return {str(k).lower() for k in deps}


def snapshot(workdir: Path, *, source: str, git_sha: str = "") -> dict:
    """저장할 형태 — 감지 결과에 **언제·무엇에서 나왔는지**를 붙인다.

    source가 필요한 이유: 같은 구조라도 리포를 훑어 얻은 것(repo)과 기획 산출물이 선언한
    것(plan)은 신뢰도가 다르다. 배포는 리포가 결정하고, 기획은 코드가 없을 때의 초기값이다.
    """
    result = detect(workdir)
    result["source"] = source
    result["detected_at"] = utcnow().isoformat()
    result["git_sha"] = git_sha
    return result


def refresh(
    db: "Session", project, workdir: Path, *, actor: str, source: str = "repo",
    git_sha: str = "",
) -> tuple[dict, bool]:
    """구조를 다시 감지해 저장하고 (구조, 바뀌었는지)를 돌려준다.

    바뀌었을 때만 커밋·감사기록을 남긴다 — 배포마다 같은 줄이 쌓이면 정작 구조가 바뀐
    순간을 찾을 수 없다. 대표 타입(type 컬럼)도 같이 맞춘다: 화면·필터·기존 API가 그
    값을 쓰고 있어서 구조만 갱신하면 둘이 어긋난다.

    감지된 컴포넌트가 하나도 없으면 **아무것도 지우지 않는다.** 체크아웃이 실패해 빈
    디렉터리를 봤을 때 사람이 확정해 둔 구조를 날리는 쪽이 더 나쁘다.
    """
    from .. import audit  # noqa: PLC0415 — 순환 임포트 방지(감사만 쓴다)

    detected = snapshot(workdir, source=source, git_sha=git_sha)
    if not detected["components"]:
        return (project.structure or detected), False
    if same_components(project.structure, detected):
        return detected, False

    before = summary(project.structure)
    project.structure = detected
    representative = representative_type(detected)
    if representative is not None:
        project.type = representative
    db.commit()
    audit.record(db, actor, "project.structure.detect", project.name, {
        "source": source, "git_sha": git_sha,
        "before": before or "(없음)", "after": summary(detected),
        "type": project.type.value,
    })
    return detected, True


def representative_type(structure: dict | None) -> ProjectType | None:
    """구조의 대표 타입 — 기존 `Project.type` 컬럼에 넣을 값.

    컴포넌트가 둘 이상이면 composite다. 하나면 그 타입 그대로. 판정 불가면 None —
    호출부가 사용자가 고른 값을 유지하거나 실패를 보고해야 한다.
    """
    components = (structure or {}).get("components") or []
    if not components:
        return None
    if len(components) > 1:
        return ProjectType.composite
    raw = components[0].get("type")
    try:
        return ProjectType(raw) if raw else None
    except ValueError:
        return None


def summary(structure: dict | None) -> str:
    """감사 로그·오류 문구에 실을 한 줄 요약: `backend=python, frontend=react`."""
    components = (structure or {}).get("components") or []
    return ", ".join(f"{c.get('name')}={c.get('type') or '판정불가'}" for c in components)


def same_components(left: dict | None, right: dict | None) -> bool:
    """배포에 영향을 주는 부분만 비교한다 — 감지 시각·커밋이 달라도 구조가 같으면 같다."""
    def key(structure: dict | None) -> list[tuple]:
        return sorted(
            (c.get("path"), c.get("type"))
            for c in ((structure or {}).get("components") or [])
        )
    return key(left) == key(right)
