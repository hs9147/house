"""등록된 레포를 검토해 **공통 제약사항·단계 프롬프트에 무엇을 더할지** 제안한다.

왜 필요한가. 공통 제약사항과 단계 프롬프트는 "우리 환경에서 반복되는 실수"를 미리 막는
장치다. 그런데 무엇이 반복되는지는 등록된 레포에 이미 남아 있다 — 비밀값이 커밋돼 있고,
리포 루트에 실행 방법이 없고, 로컬 절대 경로가 코드에 박혀 있다. 그걸 사람이 매번 발견해
문구를 손으로 고치는 대신, 근거와 함께 제안한다.

**LLM을 쓰지 않는다.** 여기서 보는 것은 전부 리포와 감지된 구조에 있는 사실이고, 제안에는
**근거(어느 프로젝트의 어느 파일)** 가 붙는다. 추측으로 만든 제약은 지켜야 할 이유를 설명할
수 없고, 그러면 아무도 지키지 않는다.

**리포는 paas-code MCP와 같은 길로 읽는다.** 워킹카피 결정은 workspace.code_workdir(없으면
한 번 체크아웃), 목록은 file_tree(`git ls-files` — 커밋된 것만), 본문은 read_file(경로 탈출·
과대 파일 차단)이다. 리포를 읽는 기능이 저마다 다른 길을 쓰면 같은 질문에 다른 답이 나오고,
한쪽에만 걸린 제한(크기·경로)이 다른 쪽에서 비어 있게 된다.

**적용은 사람이 한다.** 공통 제약사항은 그 자리에서 추가할 수 있고(관리자), 단계 프롬프트는
코드 상수라서(services/planning.STAGES) 제안 문구만 내놓는다 — 플랫폼이 제 소스를 고치게
만들면 무엇이 왜 바뀌었는지 git에 남지 않는다.

이미 등록된 제약과 겹치는 제안은 내지 않는다. 같은 말을 두 번 싣는 것은 제약을 읽는 쪽에
비용만 늘린다.
"""
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PlanConstraint, Project
from . import workspace

# 실행 방법을 알려 주는 파일 — start.cmd·구조 감지와 같은 목록이어야 한다.
ENTRY_MARKERS = ("package.json", "requirements.txt", "main.py", "app.py", "index.html")
SECRET_FILES = (".env", ".env.local", ".env.production")
JUNK_DIRS = ("__pycache__", "node_modules", ".venv", "dist", "build")
# 로컬 절대 경로 — 이 환경에서 실제로 반복된 형태(Windows 드라이브 문자).
LOCAL_PATH_HINTS = ("D:/", "D:\\", "C:/Users", "C:\\Users")
SCAN_EXTS = frozenset({".py", ".js", ".ts", ".tsx", ".jsx"})
MAX_SCAN_FILES = 400


def _findings_for(workdir: Path) -> dict:
    """리포 하나에서 뽑는 사실들 — paas-code와 같은 함수로 읽는다."""
    facts = {"no_entry": False, "secrets": [], "junk": [], "local_paths": []}
    if not workdir.is_dir():
        return facts
    facts["no_entry"] = not any((workdir / name).is_file() for name in ENTRY_MARKERS)
    scanned = 0
    for rel in workspace.file_tree(workdir, limit=2000):
        path = Path(rel)
        if path.name in SECRET_FILES:
            facts["secrets"].append(rel)
        junk = next((p for p in path.parts if p in JUNK_DIRS), "")
        if junk:
            facts["junk"].append(junk)
            continue  # 생성물 안은 읽지 않는다 — 우리 코드가 아니다
        # 본문까지 읽는 것은 코드 파일만, 그리고 상한까지만 — 레포가 크면 여기서 시간이 간다.
        if path.suffix.lower() in SCAN_EXTS and scanned < MAX_SCAN_FILES:
            scanned += 1
            try:
                # paas-code의 read_file과 같은 함수 — 경로 탈출·과대 파일을 여기서 막는다.
                text = workspace.read_file(workdir, rel)
            except (FileNotFoundError, ValueError, OSError):
                continue
            if any(hint in text for hint in LOCAL_PATH_HINTS):
                facts["local_paths"].append(rel)
    facts["junk"] = sorted(set(facts["junk"]))
    return facts


def _proposal(kind: str, text: str, reason: str, evidence: list[str],
              stage: str = "") -> dict:
    return {"kind": kind, "stage": stage, "text": text, "reason": reason,
            "evidence": evidence[:8], "evidence_count": len(evidence)}


def survey(db: Session) -> dict:
    """등록된 레포를 훑어 제안 목록을 만든다.

    반환에 `inspected`·`skipped`를 함께 싣는다 — 워킹카피가 없는 프로젝트는 보지 않으므로,
    "제안이 없다"가 "문제가 없다"인지 "보지 못했다"인지 구분돼야 한다.
    """
    projects = list(db.execute(select(Project).order_by(Project.name)).scalars())
    existing = {c.text.strip() for c in db.execute(select(PlanConstraint)).scalars()}

    inspected: list[str] = []
    skipped: list[str] = []
    no_entry: list[str] = []
    secrets: list[str] = []
    junk: list[str] = []
    local_paths: list[str] = []
    types: dict[str, int] = {}

    for project in projects:
        # paas-code MCP와 같은 규칙 — 워킹카피가 없으면 한 번 가져온다. 가져올 수 없으면
        # "보지 못했다"로 남긴다(제안이 없는 것과 문제가 없는 것은 다르다).
        try:
            workdir = workspace.code_workdir(project)
        except Exception:  # noqa: BLE001 — 리포 하나가 막혀도 검토 전체는 계속한다
            skipped.append(project.name)
            continue
        if not workdir.is_dir():
            skipped.append(project.name)
            continue
        inspected.append(project.name)
        facts = _findings_for(workdir)
        if facts["no_entry"]:
            no_entry.append(project.name)
        secrets += [f"{project.name}: {p}" for p in facts["secrets"]]
        junk += [f"{project.name}: {p}/" for p in facts["junk"]]
        local_paths += [f"{project.name}: {p}" for p in facts["local_paths"]]
        for component in (project.structure or {}).get("components") or []:
            kind = str(component.get("type") or "판정불가")
            types[kind] = types.get(kind, 0) + 1

    proposals: list[dict] = []
    if secrets:
        proposals.append(_proposal(
            "common_constraint",
            "비밀값(.env·API 키·자격증명)을 리포에 커밋하지 않는다 — 값은 플랫폼 환경변수로만 주입한다.",
            f"등록된 레포 {len({e.split(':')[0] for e in secrets})}개에 .env가 커밋돼 있다.",
            secrets,
        ))
    if local_paths:
        proposals.append(_proposal(
            "common_constraint",
            "로컬 절대 경로(D:\\ · C:\\Users 등)를 코드에 박지 않는다 — 경로는 환경변수·설정으로 받는다.",
            f"코드 {len(local_paths)}곳에 로컬 절대 경로가 있다 — 배포 서버에는 그 경로가 없다.",
            local_paths,
        ))
    if junk:
        proposals.append(_proposal(
            "common_constraint",
            "__pycache__ · node_modules · .venv 같은 생성물은 .gitignore로 제외한다.",
            f"등록된 레포 {len({e.split(':')[0] for e in junk})}개가 생성물을 커밋하고 있다.",
            junk,
        ))
    if no_entry:
        proposals.append(_proposal(
            "stage_prompt",
            "'배포 및 사용 가이드'에 **리포 루트의 실행 진입점**을 명시하라: "
            f"{' · '.join(ENTRY_MARKERS)} 중 어느 것을 두는지, 소스가 하위 폴더면 그 폴더 이름까지. "
            "플랫폼은 이 파일들로 실행 방법을 정한다(없으면 배포가 실패한다).",
            f"레포 {len(no_entry)}개가 루트에 실행 방법을 알려 주는 파일이 없다 — 배포가 실패한다.",
            no_entry,
            stage="principles",
        ))
    if types:
        top = sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))
        summary = ", ".join(f"{name} {count}" for name, count in top)
        if top[0][0] != "판정불가":
            proposals.append(_proposal(
                "stage_prompt",
                f"이 환경에서 실제로 쓰이는 배포 타입은 {summary}다. "
                f"'솔루션 구성'에서 배포 형상을 정할 때 {top[0][0]}를 기준으로 삼고, "
                "다른 타입을 고르면 그 이유를 문서에 남기게 하라.",
                f"감지된 배포 단위의 타입 분포: {summary}.",
                [f"{name}: {count}건" for name, count in top],
                stage="solution",
            ))

    # 이미 등록된 제약과 같은 말은 내지 않는다.
    proposals = [p for p in proposals
                 if not (p["kind"] == "common_constraint" and p["text"].strip() in existing)]
    return {
        "inspected": inspected,
        "skipped": skipped,
        "type_counts": types,
        "proposals": proposals,
    }
