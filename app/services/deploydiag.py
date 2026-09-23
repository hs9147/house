"""배포 실패 진단 — 무엇이 왜 실패했고 **무엇을 바꾸면 되는지**를 결정론으로 짚는다.

배포가 실패하면 화면에는 오류 한 줄과 로그 파일이 남는다. 그것만으로는 "리포가 잘못됐는가 ·
설정이 잘못됐는가 · 플랫폼이 잘못됐는가"가 갈리지 않아서, 사람은 로그를 열어 읽고 추측한다.

여기서는 **리포와 로그를 실제로 보고** 알려진 원인을 짚는다. LLM을 쓰지 않는다: 원인이 되는
사실(시그니처 파일이 있나, 진입 파일이 있나, 소스가 하위 폴더에 있나)은 전부 파일 시스템에
있고, 추측이 섞이면 "고쳤다는데 또 실패하는" 쪽이 된다.

**고침은 제안까지만 한다.** 제안은 두 종류다:
  - `source_subdir` 지정 — 소스가 하위 폴더에 있을 때. 플랫폼이 곧 배포 스크립트를 그 폴더
    기준으로 다시 만든다(build.py의 컨텍스트 결정과 start.cmd 생성이 그 값을 따른다).
  - 리포 수정 — 시그니처 파일·진입 파일이 아예 없을 때. 플랫폼이 대신 만들 수 없다.
적용과 재시도는 사람이 확인한다(api/projects의 진단 엔드포인트 → 콘솔 확인/취소).
"""
from pathlib import Path

# start.cmd가 실행 방법을 고를 때 보는 파일들(build.py의 분기와 같은 순서·같은 근거).
ENTRY_MARKERS = ("package.json", "requirements.txt", "main.py", "app.py", "index.html")
# streamlit 앱의 진입 파일 후보(start.cmd의 탐색 순서와 같다).
STREAMLIT_ENTRIES = ("app/streamlit_app.py", "streamlit_app.py", "app.py", "main.py")
# 하위 폴더 후보를 찾을 깊이. 너무 깊이 뒤지면 node_modules 같은 곳까지 제안하게 된다.
SEARCH_DEPTH = 2
SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "dist", "build", "__pycache__",
                       "docs", "sample_data", ".idea", ".vscode"})


def _has_any(directory: Path, names: tuple[str, ...]) -> list[str]:
    return [n for n in names if (directory / n).is_file()]


def _candidate_subdirs(workdir: Path) -> list[str]:
    """시그니처 파일을 가진 하위 폴더 — 소스가 거기 있다는 뜻이다."""
    found: list[str] = []
    for depth in range(1, SEARCH_DEPTH + 1):
        for path in workdir.glob("/".join(["*"] * depth)):
            if not path.is_dir() or path.name in SKIP_DIRS:
                continue
            if any(part in SKIP_DIRS for part in path.relative_to(workdir).parts):
                continue
            if _has_any(path, ENTRY_MARKERS):
                found.append(path.relative_to(workdir).as_posix())
    return sorted(set(found))


def _log_tail(log_path: str | None, limit: int = 2000) -> str:
    if not log_path:
        return ""
    try:
        return Path(log_path).read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def diagnose(workdir: Path, *, source_subdir: str = "", is_streamlit: bool = False,
             error: str = "", log_path: str | None = None) -> dict:
    """실패 원인 하나와 제안을 돌려준다.

    반환:
      cause   기계가 읽는 원인 코드("no_entry_marker" 등) — 화면 문구를 코드에서 고르지 않게.
      detail  사람이 읽는 한 문장
      fix     {"kind": "source_subdir", "value": "api"} | {"kind": "repo", ...} | None
      log_tail 로그 꼬리(사람이 확인할 근거)
    """
    base = workdir / source_subdir if source_subdir else workdir
    result = {"cause": "unknown", "detail": "", "fix": None, "log_tail": _log_tail(log_path)}

    if not base.is_dir():
        result["cause"] = "missing_source_dir"
        result["detail"] = (
            f"빌드 대상 폴더가 없습니다: {source_subdir or '(리포 루트)'} — "
            "source_subdir가 리포에 실제로 있는 폴더인지 확인하세요."
        )
        return result

    markers = _has_any(base, ENTRY_MARKERS)
    if not markers:
        # 소스가 하위 폴더에 있는지 본다 — 이 경우 플랫폼이 고칠 수 있다.
        candidates = _candidate_subdirs(base)
        if candidates:
            result["cause"] = "source_in_subdir"
            result["detail"] = (
                f"{'리포 루트' if not source_subdir else source_subdir}에 실행 방법을 알려 주는 "
                f"파일이 없고, 하위 폴더에 있습니다: {', '.join(candidates)}. "
                "빌드 대상 폴더를 그쪽으로 지정하면 배포 스크립트가 그 폴더 기준으로 다시 만들어집니다."
            )
            result["fix"] = {"kind": "source_subdir", "value": candidates[0],
                             "options": candidates}
            return result
        result["cause"] = "no_entry_marker"
        result["detail"] = (
            "실행 방법을 알 수 없습니다 — 리포에 "
            f"{' · '.join(ENTRY_MARKERS)} 중 하나가 필요합니다. 플랫폼이 대신 만들 수 없습니다."
        )
        result["fix"] = {"kind": "repo", "needs": list(ENTRY_MARKERS)}
        return result

    if is_streamlit and "requirements.txt" in markers:
        if not any((base / e).is_file() for e in STREAMLIT_ENTRIES):
            result["cause"] = "streamlit_entry_missing"
            result["detail"] = (
                "streamlit 앱인데 진입 파일이 없습니다 — "
                f"{' · '.join(STREAMLIT_ENTRIES)} 중 하나가 필요합니다."
            )
            result["fix"] = {"kind": "repo", "needs": list(STREAMLIT_ENTRIES)}
            return result

    # 시그니처는 있다 — 그러면 실패는 설치·빌드 단계에서 났을 가능성이 크다. 로그가 근거다.
    tail = result["log_tail"]
    if "npm error" in tail or "npm ERR!" in tail:
        result["cause"] = "npm_failed"
        result["detail"] = "npm 단계에서 실패했습니다 — 아래 로그 꼬리를 확인하세요."
        return result
    if "ERROR: Could not find a version" in tail or "No matching distribution" in tail:
        result["cause"] = "pip_failed"
        result["detail"] = (
            "pip가 의존성을 찾지 못했습니다 — requirements.txt의 이름·버전, 또는 사내 "
            "인덱스 설정을 확인하세요."
        )
        return result
    result["cause"] = "build_failed"
    result["detail"] = (
        f"실행 방법은 정해졌지만({', '.join(markers)}) 빌드·설치가 실패했습니다. "
        f"{'배포 오류: ' + error.strip()[:200] if error else '아래 로그 꼬리를 확인하세요.'}"
    )
    return result
