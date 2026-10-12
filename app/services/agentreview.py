"""에이전트 검토 — release로 떠 있는 프로젝트 앱이 **무엇을 하는지** LLM이 리포를 읽고 요약한다.

스마트워크는 에이전트를 이름과 종류(streamlit)만 보고 골랐다. 그러면 이름이 조금만 스쳐도
그 앱을 띄운다 — 협력사·구매 얘기면 무엇이든 supplier-pool이 열렸다. 고르는 근거가 될
목적·하는 일·**하지 않는 일**을 리포에서 뽑아 둔다(models.AgentProfile).

**무엇을 읽나.** 배포된 커밋(Deployment.git_sha)의 파일을 `git show`로 읽는다 — 워킹카피는
기획 세션이 다른 브랜치로 옮겨 두었을 수 있다(workspace.read_file_at_ref와 같은 이유). 사람이
쓴 설명(README·문서·기획서)을 먼저, 그다음 진입 파일과 의존성 선언을 글자 예산 안에서 넣는다.
무엇을 넣었는지는 sources에 적는다 — 모델이 봤다고 말하는 것이 아니라 서버가 넣은 것이 근거다.

**언제 돌리나.** 스마트워크가 에이전트 목록을 만들 때, 프로필이 없거나 배포 커밋이 바뀐 것만
뒤에서 돌린다(schedule). 그 턴은 기다리지 않는다 — 목록 하나 때문에 대화가 수십 초 멈추면
안 된다. 배포 경로가 여럿(단일·복합·재기동)이라 배포 쪽 여러 곳에 훅을 거는 것보다 쓰는 쪽
한 곳이 빠짐이 없다. 프로젝트 개요의 「다시 검토」는 review()를 바로 부른다.
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import AgentProfile, Project, utcnow
from . import workspace

# LLM에 넣는 글자 예산 — 파일 하나와 전체. 설명 문서가 앞에 오므로 잘리는 것은 코드 쪽이다.
MAX_FILE_CHARS = 8_000
MAX_SOURCE_CHARS = 30_000
MAX_TREE = 200
# 검토가 실패했으면 이만큼 지나서 다시 — 실패가 계속되는 동안(기본 LLM 없음·LLM 장애)
# 대화 턴마다 검토를 거는 일을 막는다.
RETRY_AFTER = timedelta(hours=1)

_SKIP_DIRS = {"node_modules", "vendor", "dist", "build", ".venv", "venv", "__pycache__",
              "tests", "test", ".git"}
_ENTRY_NAMES = {"app.py", "main.py", "streamlit_app.py", "server.py", "index.js", "index.ts",
                "app.tsx", "app.jsx", "main.tsx", "main.jsx"}
_MANIFESTS = {"package.json", "requirements.txt", "pyproject.toml"}
_CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}

PROMPT = """You review an internal business web app ("agent") from its repository, so that an
assistant can decide whether to open it for a user's request.

Return ONE JSON object and nothing else:
{"purpose": "...", "does": ["..."], "does_not": ["..."], "inputs": ["..."]}

- purpose: one Korean sentence - what job this app does and for whom.
- does: up to 6 short Korean phrases - concrete tasks a user can finish in this app.
- does_not: up to 4 short Korean phrases - nearby tasks someone might expect from the app's
  name or domain but which this app does NOT do. This is what keeps the assistant from opening
  it for the wrong request. Only from evidence in the files; leave empty if unclear.
- inputs: up to 4 short Korean phrases - what the user must enter or upload.

Rules:
- Use only the given files. Do not infer features from the app name.
- If the files say little, say so in purpose (e.g. "설명 문서가 거의 없음 - 화면 코드로 보면 ...").
"""


class ReviewError(Exception):
    """사람에게 그대로 보여 줄 수 있는 사유."""


def _rank(path: str) -> int | None:
    """넣을 순서 — 사람이 쓴 설명이 먼저다. 넣지 않을 파일이면 None."""
    parts = [p.lower() for p in PurePosixPath(path).parts]
    if any(p in _SKIP_DIRS for p in parts[:-1]):
        return None
    name = parts[-1]
    if name.startswith("readme"):
        return 0
    if name.endswith(".md"):
        return 1
    suffix = PurePosixPath(name).suffix
    if name in _ENTRY_NAMES or (len(parts) > 1 and parts[-2] in ("pages", "routes")
                                and suffix in _CODE_SUFFIXES):
        return 2
    if name in _MANIFESTS:
        return 3
    return None


def pick_sources(paths: list[str], subdir: str = "") -> list[str]:
    """넣을 파일을 순서대로. 하위 폴더 배포(source_subdir)면 그 폴더 것과 루트의 README·문서만."""
    prefix = subdir.strip("/") + "/" if subdir.strip("/") else ""
    ranked = []
    for path in paths:
        rank = _rank(path)
        if rank is None:
            continue
        if prefix and not path.startswith(prefix) and not (rank <= 1 and "/" not in path):
            continue
        ranked.append((rank, path.count("/"), path))
    return [p for _, _, p in sorted(ranked)]


def _gather(workdir, project: Project, git_sha: str) -> tuple[list[str], str]:
    """(넣은 파일들, LLM에 줄 글) — 예산을 넘는 파일은 넣지 않고 넘어간다."""
    tree = workspace.files_at_ref(workdir, git_sha)
    if not tree:
        raise ReviewError(f"배포된 커밋({git_sha[:12]})을 워킹카피에서 찾지 못했습니다.")
    used: list[str] = []
    blocks: list[str] = []
    budget = MAX_SOURCE_CHARS
    for path in pick_sources(tree, project.source_subdir or ""):
        body = workspace.read_file_at_ref(workdir, git_sha, path)
        if not body or not body.strip():
            continue
        body = body[:MAX_FILE_CHARS]
        if len(body) > budget:
            continue
        budget -= len(body)
        used.append(path)
        blocks.append(f"--- {path} ---\n{body}")
    if not used:
        raise ReviewError("읽을 설명·코드가 없습니다(README·문서·진입 파일).")
    head = [f"app: {project.name} (type: {project.type.value})", "",
            f"--- file list (up to {MAX_TREE}) ---", *tree[:MAX_TREE], ""]
    return used, "\n".join(head + blocks)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _parse(text: str) -> dict:
    """JSON 객체를 꺼낸다 — 코드펜스와 앞뒤 설명을 견딘다(workflowchat._parse와 같은 사정)."""
    cleaned = _FENCE_RE.sub("", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    try:
        data = json.loads(cleaned[start:end + 1] if start >= 0 and end > start else cleaned)
    except json.JSONDecodeError as e:
        raise ReviewError(f"검토 결과를 읽지 못했습니다(JSON 아님): {text[:120]}") from e
    if not isinstance(data, dict) or not str(data.get("purpose") or "").strip():
        raise ReviewError("검토 결과에 목적(purpose)이 없습니다.")
    return data


def _phrases(raw, limit: int) -> list[str]:
    return [str(x).strip()[:120] for x in raw if str(x).strip()][:limit] if isinstance(raw, list) else []


def get(db: Session, project_id: int) -> AgentProfile | None:
    return db.execute(select(AgentProfile).where(AgentProfile.project_id == project_id)).scalar_one_or_none()


def review(db: Session, project: Project, git_sha: str) -> AgentProfile:
    """검토 한 번 — 실패해도 행은 남긴다(사유를 error에). 이전 요약은 지우지 않는다."""
    from . import llm as llm_service  # noqa: PLC0415 — 순환 import 회피

    row = get(db, project.id)
    if row is None:
        row = AgentProfile(project_id=project.id)
        db.add(row)
    provider = llm_service.default_provider(db)
    try:
        if provider is None:
            raise ReviewError("기본 LLM 프로바이더가 없습니다 — LLM 관리에서 기본값을 지정하세요.")
        workdir = workspace.code_workdir(project)
        used, facts = _gather(workdir, project, git_sha)
        text = llm_service.chat_completion(
            provider, [{"role": "system", "content": PROMPT}, {"role": "user", "content": facts}], db)
        data = _parse(text)
        row.purpose = str(data["purpose"]).strip()[:500]
        row.does = _phrases(data.get("does"), 6)
        row.does_not = _phrases(data.get("does_not"), 4)
        row.inputs = _phrases(data.get("inputs"), 4)
        row.sources = used
        row.provider, row.model = provider.name, provider.model
        row.error = None
    except Exception as e:  # noqa: BLE001 — 검토가 실패해도 대화·배포는 그대로다
        row.error = str(e)[:500] or type(e).__name__
    row.git_sha = git_sha
    row.reviewed_at = utcnow()
    db.commit()
    return row


def stale(row: AgentProfile | None, git_sha: str, now: datetime | None = None) -> bool:
    """다시 검토할 때인가 — 없거나, 배포 커밋이 바뀌었거나, 실패한 지 RETRY_AFTER가 지났다."""
    if row is None or row.git_sha != git_sha:
        return True
    if not row.error:
        return False
    at = row.reviewed_at if row.reviewed_at.tzinfo else row.reviewed_at.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - at >= RETRY_AFTER


# 뒤에서 돌리는 검토는 하나씩 — 배포 작업 큐(services/jobs)와 따로 둔다. 같은 큐에 넣으면
# 수십 초짜리 LLM 호출이 배포 일꾼을 붙든다.
_executor: ThreadPoolExecutor | None = None
_pending: set[int] = set()
_lock = threading.Lock()


def _submit(fn, *args) -> None:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paas-agentreview")
    _executor.submit(fn, *args)


def schedule(project_id: int, git_sha: str) -> None:
    """뒤에서 검토한다 — 이미 걸려 있으면 다시 걸지 않는다."""
    with _lock:
        if project_id in _pending:
            return
        _pending.add(project_id)
    _submit(_job, project_id, git_sha)


def _job(project_id: int, git_sha: str) -> None:
    try:
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            if project is not None:
                review(db, project, git_sha)
    except Exception as e:  # noqa: BLE001
        print(f"[paas] 에이전트 검토 실패(무시): {project_id} — {e}")
    finally:
        with _lock:
            _pending.discard(project_id)
