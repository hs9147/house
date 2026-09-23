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

from ..models import BuildProfile, LlmProvider, Project
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

# 포트는 플랫폼이 정한다 — 프록시가 그 포트로 보내고 헬스체크도 그 포트를 본다. 스크립트가
# 다른 포트로 뜨면 배포는 "성공"인데 주소는 502다. 그래서 형태를 확인한다.
#
# **왜 형태를 따지는가.** 실측에서 LLM이 `$PORT`·`$env:PORT`처럼 셸이 다른 문법을 썼다.
# cmd 배치에서 그것은 값이 아니라 글자라, 앱은 기본 포트로 뜨고 아무도 그 사실을 모른다.
# 그래서 "왜 거부됐는지"가 아니라 "무엇을 쓰면 되는지"를 말한다(사람과 다음 LLM 시도가
# 같은 문장을 읽는다).
_DELAYED_EXPANSION = re.compile(r"setlocal\s+[^\n]*enabledelayedexpansion", re.IGNORECASE)
_WRONG_PORT_FORMS = (
    (r"\$env:PORT", "$env:PORT (PowerShell)"),
    (r"\$\{PORT\}", "${PORT} (POSIX 셸)"),
    (r"\$PORT\b", "$PORT (POSIX 셸)"),
    (r"process\.env\.PORT", "process.env.PORT (Node 코드)"),
)


# PORT에 값을 넣는 줄. 세 가지는 괜찮다: `if not defined PORT set PORT=8000`(값이 없을 때의
# 기본값 — 배포에서는 늘 정의돼 있다), `set PORT=%PORT%`(제자리 대입, 플랫폼 템플릿이 쓴다),
# 그리고 값 안에 PORT를 참조하는 형태. 그 밖의 대입은 주입된 포트를 **버린다** — %PORT%를
# 쓰고 있어도 그 값이 8000이면 프록시가 보는 포트가 아니고, 배포는 성공인데 주소는 502다.
_PORT_ASSIGN = re.compile(r"\bset\s+(?:/a\s+)?PORT\s*=\s*(\S[^\r\n]*)", re.IGNORECASE)


def _port_overwrite_problem(commands: str) -> str:
    for line in commands.splitlines():
        found = _PORT_ASSIGN.search(line)
        if not found:
            continue
        if re.match(r"\s*if\s+not\s+defined\s+PORT\b", line, re.IGNORECASE):
            continue  # 기본값 — 주입된 값이 있으면 실행되지 않는다
        if re.search(r"[%!]PORT[%!]", found.group(1), re.IGNORECASE):
            continue  # 자기 값을 다시 넣는 것은 버리는 것이 아니다
        return ("PORT를 스크립트가 덮어씁니다 — 플랫폼이 주입한 포트를 그대로 써야 합니다"
                f"(문제 줄: `{line.strip()[:80]}`). 기본값이 필요하면 "
                "`if not defined PORT set PORT=8000` 형태로만 쓰세요.")
    return ""


def _port_problem(commands: str) -> str:
    """리슨 포트를 플랫폼이 준 값으로 쓰는가 — 문제가 없으면 빈 문자열."""
    overwritten = _port_overwrite_problem(commands)
    if overwritten:
        return overwritten
    upper = commands.upper()  # cmd의 변수 이름은 대소문자를 가리지 않는다
    if "%PORT%" in upper:
        return ""
    # !PORT!는 지연 확장이 켜져 있을 때만 값이 된다 — 켜지 않고 쓰면 글자 그대로 남는다.
    if "!PORT!" in upper:
        if _DELAYED_EXPANSION.search(commands):
            return ""
        return ("!PORT!는 지연 확장이 켜져 있어야 값이 들어갑니다 — 첫 줄에 "
                "`setlocal enabledelayedexpansion`을 넣거나 %PORT%를 쓰세요.")
    for pattern, shown in _WRONG_PORT_FORMS:
        if re.search(pattern, commands, re.IGNORECASE):
            return (f"포트를 {shown} 형태로 썼습니다 — 이 파일은 cmd 배치입니다. "
                    "명령줄에 %PORT%를 그대로 쓰세요(예: `--port %PORT% --host %HOST%`).")
    return ("리슨 포트(%PORT%)를 쓰지 않습니다 — 플랫폼이 준 포트로 떠야 합니다"
            "(예: `--port %PORT% --host %HOST%`). 앱이 환경변수 PORT를 스스로 읽더라도, "
            "명령줄에 %PORT%를 적어 무엇으로 뜨는지 스크립트에 드러내세요.")

_FENCE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)

SYSTEM_PROMPT = """You write a Windows batch script (start.cmd) that launches one already
checked-out repository on a given port. Reply with the script only, inside one ```bat fence.

Hard rules:
- The command that starts the app MUST contain the literal cmd variables %PORT% and %HOST%,
  for example: `python -m uvicorn app.main:app --host %HOST% --port %PORT%`.
  This is a cmd batch file: $PORT, ${PORT}, $env:PORT and process.env.PORT are plain text
  here, not values. Never hardcode a port number. A platform check rejects the script when
  %PORT% is missing, because the proxy and the health check only look at that port - an app
  on any other port looks "deployed" and answers 502.
  If the app reads PORT from the environment on its own, still pass it on the command line
  so the script says which port it comes up on.
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


def _facts(workdir: Path, project: Project, component: str = "",
           profile: BuildProfile = BuildProfile.release) -> str:
    """프롬프트에 실을 **사실**. 리포에서 읽은 것만 넣는다(프로필은 플랫폼의 사실이다)."""
    files = workspace.file_tree(workdir, limit=MAX_FILES_IN_PROMPT)
    detected = structure.detect(workdir)
    base = component_dir(workdir, project, component)
    lines = [
        f"project: {project.name}",
        f"declared type: {project.type.value}",
        f"detected components: {structure.summary(detected) or '(none)'}",
        (f"this unit: component '{component}'"
         if component else "this unit: the whole repo"),
        # 프로필이 기동 방법을 정한다 — 같은 리포에 두 스크립트가 필요한 이유다.
        f"deploy profile: {profile.value}",
        ("this profile serves the built output (build once at deploy time, then serve it)"
         if profile != BuildProfile.development else
         "this profile runs the project's dev server (HMR); %PAAS_BASE_PATH% is the "
         "public sub-path the proxy forwards WITHOUT stripping, so pass it as the base"),
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
    port = _port_problem(commands)
    if port:
        problems.append(port)
    return problems


def _extract(reply: str) -> str:
    """응답에서 스크립트만 꺼낸다 — 펜스가 있으면 그 안, 없으면 전체."""
    found = _FENCE.search(reply)
    return (found.group(1) if found else reply).strip()


def propose(db: Session, project: Project, provider: LlmProvider, workdir: Path,
            component: str = "", profile: BuildProfile = BuildProfile.release) -> dict:
    """LLM에게 기동 스크립트를 쓰게 하고 **검증 결과와 함께** 돌려준다(저장하지 않는다).

    복합 배포는 컴포넌트마다 따로 부른다 — 한 스크립트가 여러 컴포넌트를 띄우면 서비스
    감시자(nssm)가 자식 하나만 보고, 헬스체크도 포트 하나만 본다. 프로필도 따로다:
    개발 배포는 dev 서버로, 운영 배포는 빌드본으로 뜬다.
    """
    facts = _facts(workdir, project, component, profile)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": facts}]
    script = _extract(llm_service.chat_completion(provider, messages, db))
    problems = validate(script)
    attempts = 1

    # **거부되면 한 번은 고치게 한다.** 검증은 저장을 막는 것이지 사람에게 숙제를 내는 것이
    # 아니다 — 실측에서 %PORT%를 빠뜨린 제안이 그대로 거부되어, 사람이 검증 문구를 읽고 손으로
    # 고쳐 넣어야 했다. 그 문구를 그대로 모델에게 돌려주면 대개 한 번에 고친다.
    # 한 번만 한다: 두 번 틀리는 모델은 세 번째도 틀리고, 그때는 사람이 보는 편이 빠르다.
    if problems:
        messages += [
            {"role": "assistant", "content": script},
            {"role": "user", "content": (
                "The platform rejected that script:\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\nReturn the whole corrected script only, in one ```bat fence."
            )},
        ]
        retried = _extract(llm_service.chat_completion(provider, messages, db))
        retried_problems = validate(retried)
        attempts = 2
        # 나빠지면 첫 제안을 지킨다 — "고쳤다"가 늘 나아짐은 아니다.
        if len(retried_problems) < len(problems):
            script, problems = retried, retried_problems

    return {"script": script, "problems": problems, "facts": facts, "attempts": attempts}
