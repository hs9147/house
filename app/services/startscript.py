"""기동 스크립트(start.cmd)를 LLM이 쓰게 한다 — 제안까지만, 저장은 사람이.

**왜 LLM인가.** 플랫폼의 제네릭 템플릿은 흔한 모양만 맞힌다(package.json·requirements.txt·
main.py·app.py·index.html). 맞지 않는 프로젝트는 조용히 엉뚱하게 뜬다 — 실측에서 streamlit이
uvicorn으로 기동을 시도했고, 시그니처가 없는 리포는 정적 서빙으로 흘렀다. 리포를 보고
"이 프로젝트는 이렇게 띄운다"를 쓰는 일은 규칙표로 다 덮을 수 없다.

**그래서 세 가지를 지킨다.**

1. **사실만 준다.** 프롬프트에 싣는 것은 리포에서 읽은 것(파일 목록, package.json scripts,
   requirements.txt 이름, 감지된 구조)뿐이다. 추측할 여지를 줄이면 나오는 스크립트도 덜 튄다.
2. **검증하고 거른다.** 이 스크립트는 서버에서 **서비스 권한으로** 실행된다. LLM이 쓴 것을
   그대로 실행하면 그것은 원격 코드 실행이다. 그래서 위험한 명령과 네트워크 실행을 막고,
   기동에 필요한 형태(%PORT%로 리슨)를 확인한다. 하나라도 걸리면 **저장을 거부한다** —
   경고만 하고 통과시키면 아무도 읽지 않는다.
3. **사람이 확인한다.** propose()는 저장하지 않는다. 화면이 스크립트와 검증 결과를 보여 주고
   사람이 확인해야 저장된다(api/projects의 start-script 경로).

LLM이 없거나 실패하면 템플릿이 그대로 쓰인다 — 배포가 LLM 가용성에 매달리면 안 된다.
"""
import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import LlmProvider, Project
from . import llm as llm_service
from . import structure, workspace

MAX_SCRIPT_CHARS = 8000
MAX_FILES_IN_PROMPT = 200

# **거부 목록.** 서비스 권한으로 도는 스크립트라 "기동에 필요 없는데 위험한 것"은 막는다.
# 완전하지 않다(거부 목록은 원래 새어 나간다) — 그래서 사람 확인을 함께 둔다.
FORBIDDEN = (
    # 파괴
    (r"\bdel\s+/[sqf]", "파일 일괄 삭제(del /s)"),
    (r"\brd\s+/s", "폴더 일괄 삭제(rd /s)"),
    (r"\brmdir\s+/s", "폴더 일괄 삭제(rmdir /s)"),
    (r"\bformat\b", "디스크 포맷"),
    (r"\bshutdown\b", "시스템 종료"),
    (r"\btaskkill\b", "다른 프로세스 종료"),
    # 밖에서 코드를 끌어와 실행 — 기동 스크립트가 할 일이 아니다
    (r"\b(curl|wget|bitsadmin|certutil)\b", "외부에서 파일을 내려받는 명령"),
    (r"Invoke-WebRequest|Invoke-Expression|\biwr\b|\biex\b", "PowerShell 원격 실행"),
    (r"-enc(odedcommand)?\b", "인코딩된 PowerShell 명령"),
    # 시스템·계정·서비스 변경
    (r"\bnet\s+(user|localgroup)\b", "계정 변경"),
    (r"\breg\s+(add|delete)\b", "레지스트리 변경"),
    (r"\bsc\s+(create|delete|config)\b", "서비스 변경"),
    (r"\bnssm\b", "서비스 등록(플랫폼이 하는 일이다)"),
    (r"\bschtasks\b", "예약 작업 등록"),
    (r"\bicacls\b|\battrib\b", "권한·속성 변경"),
    # 리포 밖으로 나가기
    (r"cd\s+/d\s+[A-Za-z]:", "절대 경로로 이동"),
    (r"\.\.[\\/]\.\.", "상위 폴더 탈출"),
)

# 기동 스크립트라면 반드시 있어야 하는 것 — 없으면 앱이 프록시가 보는 포트에서 뜨지 않는다.
REQUIRED = ((r"%PORT%", "리슨 포트(%PORT%)를 쓰지 않습니다 — 플랫폼이 준 포트로 떠야 합니다"),)

_FENCE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)

SYSTEM_PROMPT = """You write a Windows batch script (start.cmd) that launches one already
checked-out repository on a given port. Reply with the script only, inside one ```bat fence.

Hard rules:
- Listen on %PORT% and bind %HOST% (the platform injects both). Never hardcode a port.
- Dependencies are already installed by the platform before this runs. Do not run
  npm ci / npm install / pip install unless the folder for them is missing.
- Do not download anything. Do not touch services, the registry, accounts or scheduled tasks.
- Do not delete files or folders recursively.
- Stay inside the repository. Do not cd to absolute paths.
- If you cannot tell how to start the app, echo what is missing and `exit /b 1`.
  A wrong guess is worse than a clear failure.
- ASCII ONLY, including comments. cmd.exe parses the file with the console codepage
  (Korean Windows uses 949); non-ASCII bytes desync its double-byte reader and the next
  line gets executed as a command. Write REM comments in English.
- You launch ONE unit. If the repo has several components, only start the one described
  as "this unit" - the platform runs each component as its own service on its own port.
"""


def component_dir(workdir: Path, project: Project, component: str = "") -> Path:
    """이 컴포넌트가 도는 폴더. 단일 배포는 source_subdir(없으면 리포 루트)다.

    복합 배포는 컴포넌트마다 폴더가 다르고, **이름이 아니라 경로**를 쓴다 — `apps/web`은
    이름이 `apps-web`이다(docker 태그에 슬래시를 넣을 수 없어 그렇게 정했다).
    """
    if component:
        for comp in (project.structure or {}).get("components") or []:
            if str(comp.get("name")) == component:
                path = str(comp.get("path") or "")
                return workdir / path if path else workdir
        raise ValueError(f"감지된 구조에 없는 컴포넌트입니다: {component}")
    return workdir / (project.source_subdir or "")


def _rel(path: Path, workdir: Path) -> str:
    return path.relative_to(workdir).as_posix() or "(repo root)"


def _facts(workdir: Path, project: Project, component: str = "") -> str:
    """프롬프트에 실을 **사실**. 리포에서 읽은 것만 넣는다."""
    files = workspace.file_tree(workdir, limit=MAX_FILES_IN_PROMPT)
    detected = structure.detect(workdir)
    base = component_dir(workdir, project, component)
    lines = [
        f"project: {project.name}",
        f"declared type: {project.type.value}",
        f"detected components: {structure.summary(detected) or '(none)'}",
        (f"this unit: component '{component}'"
         if component else "this unit: the whole repo"),
        # 스크립트가 어느 폴더에서 실행되는지 명시한다 — 복합 배포는 컴포넌트 폴더에서
        # 돌지만(런타임이 그 폴더를 실행 폴더로 잡는다), 단일 배포는 리포 루트에서 돈다.
        # 이 둘을 섞으면 "cd 없이 package.json이 있다고 믿는" 스크립트가 나온다.
        f"start.cmd runs with this folder as the working directory: "
        f"{_rel(base if component else workdir, workdir)}",
        f"this unit's files are in: {_rel(base, workdir)}",
        "",
        f"files (git ls-files, up to {MAX_FILES_IN_PROMPT}):",
        *[f"  {f}" for f in files],
    ]
    package = base / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
            lines += ["", "package.json scripts:",
                      *[f"  {k}: {v}" for k, v in scripts.items()]]
        except (OSError, ValueError):
            lines.append("package.json: (읽을 수 없음)")
    requirements = base / "requirements.txt"
    if requirements.is_file():
        try:
            names = [ln.strip() for ln in
                     requirements.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
            lines += ["", "requirements.txt:", *[f"  {n}" for n in names[:60]]]
        except OSError:
            pass
    return "\n".join(lines)


def validate(script: str) -> list[str]:
    """스크립트의 문제 목록. 비어 있으면 저장해도 되는 것으로 본다.

    거부 목록은 완전하지 않다 — 그래서 이것만으로 안전을 주장하지 않고, 사람 확인과 함께 쓴다.
    """
    problems: list[str] = []
    text = script.strip()
    if not text:
        return ["스크립트가 비어 있습니다."]
    if len(text) > MAX_SCRIPT_CHARS:
        problems.append(f"너무 깁니다({len(text)}자, 최대 {MAX_SCRIPT_CHARS}) — 기동 스크립트가 아닙니다.")
    # **ASCII만 허용한다.** cmd.exe는 이 파일을 콘솔 코드페이지(한국어 윈도우는 949)로
    # 읽는다. 한글이 섞이면 2바이트 판독이 어긋나 줄 경계가 밀리고, 다음 줄이 명령으로
    # 실행된다 — 실측에서 UTF-8 한글 주석이 든 start.cmd는 기동 전에 rc=255로 죽었다
    # (그것이 nssm의 "SERVICE_PAUSED"로 보였다). 주석까지 영어로 써야 한다.
    if not text.isascii():
        outside = sorted({c for c in text if not c.isascii()})
        problems.append(
            "ASCII 문자만 쓸 수 있습니다 — cmd가 콘솔 코드페이지로 읽어 줄 경계가 밀립니다"
            f"(문제 문자: {' '.join(outside[:8])})"
        )
    # 주석(REM)은 판정에서 뺀다 — "예전에는 curl을 썼다" 같은 설명이 걸리면 안 된다.
    commands = "\n".join(
        ln for ln in text.splitlines() if not ln.strip().upper().startswith("REM")
    )
    for pattern, label in FORBIDDEN:
        if re.search(pattern, commands, re.IGNORECASE):
            problems.append(f"허용하지 않는 명령: {label}")
    for pattern, message in REQUIRED:
        if not re.search(pattern, commands, re.IGNORECASE):
            problems.append(message)
    return problems


def _extract(reply: str) -> str:
    """응답에서 스크립트만 꺼낸다 — 펜스가 있으면 그 안, 없으면 전체."""
    found = _FENCE.search(reply)
    return (found.group(1) if found else reply).strip()


def propose(db: Session, project: Project, provider: LlmProvider, workdir: Path,
            component: str = "") -> dict:
    """LLM에게 기동 스크립트를 쓰게 하고 **검증 결과와 함께** 돌려준다(저장하지 않는다).

    복합 배포는 컴포넌트마다 따로 부른다 — 한 스크립트가 여러 컴포넌트를 띄우면 서비스
    감시자(nssm)가 자식 하나만 보고, 헬스체크도 포트 하나만 본다.
    """
    facts = _facts(workdir, project, component)
    reply = llm_service.chat_completion(
        provider,
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": facts}],
        db,
    )
    script = _extract(reply)
    return {"script": script, "problems": validate(script), "facts": facts}
