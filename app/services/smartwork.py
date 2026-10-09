"""스마트워크 — 대화 한 턴을 도구와 함께 돌리고, 오른쪽 화면에 무엇을 띄울지 정한다.

화면은 모델이 **도구로** 고른다(show_agent·show_report). 답변 본문에서 "에이전트를
열어 드릴게요" 같은 문구를 찾아 화면을 바꾸면 말투가 조금만 달라져도 어긋난다 — 도구
호출은 이름과 인자가 정해진 신호라 화면이 그것만 따르면 된다.

사내 MCP 도구(문서·온톨로지·API·코드·운영)는 api/smartwork.py가 골라 넘긴다. 이 모듈은
그것들을 하나의 도구 목록으로 묶고, 그 사람만의 개인 도구(services/personal.py의
저장소)를 **그 사람의 이메일에 묶어** 붙인다 — 개인 도구에는 누구의 저장소인지 고르는
인자가 없다.
"""
import base64
import binascii
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..models import (
    ApiKey, BuildProfile, Deployment, DeploymentStatus, Organization, Project, Workflow, WorkflowRun,
    WorkflowRunStatus,
)
from ..security import viewer_org_ids
from . import docready, docsearch, doctext, mcp_server, personal
from . import llm as llm_service
from . import storage as storage_service
from . import workflow as workflow_service
from .proxy import domain_for, path_prefix_for

# (접두사, MCP 모양 도구 목록, 호출(도구명, 인자) -> 문자열)
Toolset = tuple[str, list[dict], Callable[[str, dict], str]]

# 모델에게 다시 보내는 대화 길이 — 화면은 전부 보여 주지만 매 턴 전부 보내면 토큰이
# 대화 길이에 비례해 는다.
MAX_HISTORY = 20
MAX_MESSAGE_CHARS = 20_000
# 개인 문서 한 편을 도구 결과로 돌려줄 최대 글자 수(사내 문서 서버와 같은 값).
_MAX_READ_CHARS = 40_000
# 보고서는 HTML 하나로 낸다 — 표·강조·여러 구역을 한 문서로 담고, 그대로 내려받아 돌려 볼 수 있다.
REPORT_FORMATS = ("html",)
MAX_SUGGESTIONS = 6
MAX_CHOICES = 6

# 대화 첨부 — 이번 요청의 참고 자료. 원본은 남기지 않는다: 문서는 읽어 낸 글만 대화에 남고
# (다음 턴에도 모델이 읽는다), 이미지는 그 턴에만 모델에게 간다.
MAX_ATTACHMENTS = 5
# 첨부는 JSON(base64)으로 온다 — IIS 기본 요청 상한(약 28MB) 안에 들도록 한 개 10MB, 합 20MB.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_TOTAL = 20 * 1024 * 1024
MAX_ATTACHMENT_CHARS = 20_000
# Bedrock Converse가 받는 이미지 한 장의 상한(3.75MB).
MAX_IMAGE_BYTES = 3_750_000
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpeg", "image/gif": "gif", "image/webp": "webp"}


class AttachmentError(ValueError):
    pass


def read_attachments(items: list[dict]) -> tuple[list[dict], list[str]]:
    """[{name, type, data(base64)}] → (대화에 남길 것 [{name, kind, text}], 이번 턴의 이미지 data URL)."""
    if len(items) > MAX_ATTACHMENTS:
        raise AttachmentError(f"첨부는 한 번에 {MAX_ATTACHMENTS}개까지입니다.")
    kept: list[dict] = []
    images: list[str] = []
    total = 0
    for item in items:
        name = Path(str(item.get("name") or "첨부")).name[:120]
        try:
            data = base64.b64decode(str(item.get("data") or ""), validate=True)
        except (binascii.Error, ValueError):
            raise AttachmentError(f"{name}: 내용을 읽을 수 없습니다.")
        total += len(data)
        if total > MAX_ATTACHMENTS_TOTAL:
            raise AttachmentError(f"첨부는 합해서 {MAX_ATTACHMENTS_TOTAL // (1024 * 1024)}MB까지입니다.")
        mime = str(item.get("type") or "").lower()
        if mime in IMAGE_TYPES:
            if len(data) > MAX_IMAGE_BYTES:
                raise AttachmentError(f"{name}: 이미지는 {MAX_IMAGE_BYTES / 1_000_000:g}MB까지입니다.")
            images.append(f"data:image/{IMAGE_TYPES[mime]};base64,{base64.b64encode(data).decode()}")
            kept.append({"name": name, "kind": "image", "text": ""})
            continue
        suffix = Path(name).suffix.lower()
        if suffix not in personal.DOC_SUFFIXES:
            raise AttachmentError(f"{name}: 첨부할 수 없는 형식입니다(문서·이미지만).")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentError(f"{name}: 파일은 {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB까지입니다.")
        # 형식 판별·추출은 파일 경로로 한다(doctext) — 읽고 나면 임시 폴더째 지운다.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"attachment{suffix}"
            path.write_bytes(data)
            try:
                text = doctext.extract_markdown(path)
            except doctext.ExtractError as e:
                raise AttachmentError(f"{name}: {e}")
        kept.append({"name": name, "kind": "document", "text": text[:MAX_ATTACHMENT_CHARS]})
    return kept, images
# 대화는 **제안으로 시작한다** — 빈 화면에 "무엇을 도와드릴까요"를 띄우면 사람이 할 일을
# 떠올려야 한다. 첫 턴에는 이 지시를 사용자 차례로 넣어 모델이 업무 맥락을 먼저 훑게 한다.
# 화면에는 나오지 않는다(콘솔은 답변부터 보여 준다).
# 워크플로는 절차일 뿐 진행할 **대상**(과제·계약·구매요청 같은 업무 단위 한 건)이 있어야
# 일이 된다 — 대상은 개인 업무 맥락(메일·문서)에서 찾고, 워크플로는 그 건의 다음 할 일을 정한다.
OPENING_PROMPT = (
    "대화를 시작한다. dept__workflows로 우리 부서 워크플로와 각 워크플로가 다루는 업무 단위"
    "(targets)를 먼저 보고, 개인 업무 맥락이 있으면 my__recent·my__search로 최근 메일·문서에서"
    " 실제 업무 단위(과제·계약·구매요청 등 — 이름이 붙은 건 하나하나)를 찾아 줘. 찾은 건마다"
    " 어느 워크플로의 어느 상태인지 판단해 다음에 할 일을 제안하고, 맡을 에이전트가 있으면"
    " 그렇다고 밝혀 줘. 대상이 없는 제안은 하지 않는다 — 찾지 못했으면 그렇다고 말하고 업무"
    " 맥락을 고르거나 대상을 알려 달라고 해. 제안은 suggest_tasks로 띄우고 대화에는 인사와"
    " 한두 줄 요약만 써."
)


def agents(db: Session, key: ApiKey) -> list[dict]:
    """이 사람이 쓸 수 있는 **에이전트** = release로 떠 있는 프로젝트 앱 중 보이는 것.

    보이는 범위는 코드·MCP 접근과 같은 규칙이다(관리자, 조직 미지정, 소속 조직).
    """
    rows = db.execute(
        select(Project).join(Deployment).where(
            Deployment.profile == BuildProfile.release,
            Deployment.status == DeploymentStatus.running,
        ).distinct().order_by(Project.name)
    ).scalars().all()
    org_ids = None if key.is_admin else viewer_org_ids(db, key)
    out = []
    for project in rows:
        if org_ids is not None and project.organization_id is not None \
                and project.organization_id not in org_ids:
            continue
        org = project.organization.name if project.organization else None
        out.append({
            "name": project.name,
            "org": org,
            "type": project.type.value,
            "domain": domain_for(project.name, BuildProfile.release),
            "path": path_prefix_for(org, project.name, BuildProfile.release),
        })
    return out


# --- 부서 워크플로 ---

def department_workflows(db: Session, key: ApiKey, org_id: int | None = None) -> list[dict]:
    """이 사람의 **부서**(소속 조직) 워크플로 — 업무 제안의 근거.

    제안을 모델의 짐작에 맡기면 "그럴듯한데 우리 부서 일이 아닌" 일이 나온다. 부서가 실제로
    하는 일의 목록은 워크플로가 이미 갖고 있다(단계·사람 작업·제약). 관리자라도 소속이 없으면
    부서가 없다 — 모든 조직의 워크플로를 섞으면 남의 부서 일을 제안한다.

    org_id는 세션의 조직이다 — 그 업무의 부서는 세션 소유자가 정했고, 다른 부서에서 공유받아
    말하는 사람에게도 같은 업무 맥락이 보여야 한다(소유자가 고른 조직인지는 api가 확인했다).
    """
    org_ids = {org_id} if org_id is not None else viewer_org_ids(db, key)
    if not org_ids:
        return []
    rows = db.execute(
        select(Workflow).where(Workflow.organization_id.in_(org_ids))
        .order_by(Workflow.organization_id, Workflow.name)
    ).scalars().all()
    out = []
    for row in rows:
        nodes = [n for n in (row.spec or {}).get("nodes", []) if isinstance(n, dict)]
        # 사람 단계에서 멈춘 실행 = 지금 누군가의 손을 기다리는 일. 제안의 1순위 근거다.
        waiting = db.execute(
            select(WorkflowRun.id).where(WorkflowRun.workflow_id == row.id,
                                         WorkflowRun.status == WorkflowRunStatus.waiting)
        ).scalars().all()
        out.append({
            "org": row.organization.name if row.organization else "",
            "name": row.name,
            "description": row.description,
            "steps": [_step(n) for n in nodes],
            "constraints": workflow_service.constraints_of(row),
            "waiting_runs": len(waiting),
            **_targets_of(row.extracted or {}),
        })
    return out


def _targets_of(extracted: dict) -> dict:
    """워크플로가 다루는 업무 단위(대화에서 읽어 낸 entities)와 그 상태·전이 — 개인 맥락에서
    찾은 건이 지금 어느 상태이고 다음에 무엇을 할지 가늠하는 잣대다."""
    def rows(key: str) -> list[dict]:
        found = extracted.get(key)
        return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []

    states = rows("states")
    return {
        "targets": [{"kind": str(e.get("name") or ""), "note": str(e.get("note") or ""),
                     "states": [str(st.get("name") or "") for st in states
                                if st.get("entity") == e.get("name")]}
                    for e in rows("entities") if e.get("name")],
        "transitions": [f"{t.get('from', '')} → {t.get('to', '')}"
                        + (f" ({t['trigger']})" if t.get("trigger") else "")
                        for t in rows("transitions")],
    }


def _step(node: dict) -> str:
    kind = str(node.get("type") or "")
    if kind == "human":
        role = node.get("role")
        return f"사람 작업: {node.get('title', '')}" + (f" ({role})" if role else "")
    label = workflow_service.NODE_TYPES.get(kind, {}).get("label", kind)
    return f"{label}: {node.get('label') or node.get('id', '')}"


_DEPT_TOOLS = [{
    "name": "workflows",
    "description": (
        "우리 부서(소속 조직)의 워크플로 — 이름·설명·단계(사람 작업 포함)·업무 규칙, 사람"
        " 단계에서 멈춰 있는 실행 수, 다루는 업무 단위(targets: 종류·상태)와 상태 전이."
        " 업무를 제안할 때 먼저 부른다."
    ),
    "inputSchema": {"type": "object", "properties": {}},
}]


# --- 개인 도구 ---

_PERSONAL_TOOLS = [
    {
        "name": "sources",
        "description": "내 업무 맥락에 무엇이 있는지 — 올린 로컬 폴더, 연결한 메일, 색인 상태.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "recent",
        "description": (
            "최근에 들어온 내 문서·메일(새것부터) — 경로와 제목. 지금 무슨 일이 진행 중인지"
            " 볼 때 먼저 부른다. 메일은 outlook/ 아래이고 파일 이름 앞이 받은 날짜다."
        ),
        "inputSchema": {"type": "object",
                        "properties": {"limit": {"type": "integer", "description": "기본 20, 최대 50"}}},
    },
    {
        "name": "search",
        "description": (
            "**내** 문서·메일 본문 검색(사내 문서와 별개). 공백으로 끊은 낱말을 모두 포함하는"
            " 것을 찾는다. 메일은 outlook/ 아래 경로로 나온다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"},
                           "limit": {"type": "integer", "description": "기본 10, 최대 30"}},
            "required": ["query"],
        },
    },
    {
        "name": "read",
        "description": "내 문서·메일 하나를 마크다운 본문으로 읽는다. path는 search 결과 값.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                        "required": ["path"]},
    },
    {
        "name": "find_nodes",
        "description": (
            "내 문서의 온톨로지에서 노드를 이름으로 찾는다. kind: document | section | term"
            " | table."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}, "kind": {"type": "string"},
                           "limit": {"type": "integer"}},
        },
    },
    {
        "name": "neighbors",
        "description": "내 온톨로지에서 노드 하나에 붙은 관계(out/in). kind·name은 find_nodes 값.",
        "inputSchema": {
            "type": "object",
            "properties": {"kind": {"type": "string"}, "name": {"type": "string"}},
            "required": ["kind", "name"],
        },
    },
]


def _limit(args: dict, default: int, cap: int) -> int:
    try:
        return max(1, min(int(args.get("limit") or default), cap))
    except (TypeError, ValueError):
        return default


# 세션에 고른 범위로 거른 뒤 개수를 맞추려고 색인에서는 넉넉히 받아 온다.
_SCOPE_FETCH = 200


def personal_toolset(db: Session, email: str, folders: list[str] | None = None,
                     mail: bool = False) -> Toolset | None:
    """동의한 사람에게만 붙는다. 저장소는 여기서 이메일로 정해지고 인자로 바뀌지 않는다.

    **세션에 고른 것만 본다** — folders(폴더 이름들)와 mail(outlook/). 하나도 고르지 않았으면
    도구를 붙이지 않는다. 업무 하나에 내 문서 전체를 열어 두면 공유 세션의 답변에 그 업무와
    무관한 내 메일이 인용될 수 있다.
    """
    if personal.get(db, email) is None:
        return None
    scope = tuple(f"{f}/" for f in folders or []) + ((f"{personal.MAIL_DIR}/",) if mail else ())
    if not scope:
        return None
    store = personal.store_for(email)

    def allowed(path: str) -> bool:
        return path.startswith(scope)

    def call(name: str, args: dict) -> str:
        if name == "sources":
            status = personal.status(db, email)
            status["folders"] = [f for f in status["folders"] if f.get("name") in (folders or [])]
            if not mail:
                status["mail"] = {"connected": False, "note": "이 세션에는 메일을 고르지 않았다"}
            status.pop("index", None)  # 저장소 전체의 수치라 이 세션 범위와 맞지 않는다
            return json.dumps(status, ensure_ascii=False, default=str)
        if name == "recent":
            return json.dumps(_recent(store.root, _limit(args, 20, 50), allowed), ensure_ascii=False)
        if name == "search":
            found = docsearch.search(store.name, str(args.get("query", "")), _SCOPE_FETCH)
            hits = [h for h in found["hits"] if allowed(h["path"])]
            limit = _limit(args, 10, 30)
            found.update(hits=hits[:limit], truncated=len(hits) > limit or found["truncated"])
            return json.dumps(found, ensure_ascii=False)
        if name == "read":
            try:
                target = storage_service.resolve(store.root, str(args.get("path", "")))
                rel = target.relative_to(store.root).as_posix()
                if not allowed(rel):
                    raise mcp_server.McpToolError(f"이 세션에 고른 폴더·메일이 아닙니다: {rel}")
                if not target.is_file():
                    raise mcp_server.McpToolError(f"파일이 없습니다: {rel}")
                text = docready.read(store.name, rel, target)
            except (storage_service.StorageError, doctext.ExtractError) as e:
                raise mcp_server.McpToolError(str(e))
            if len(text) > _MAX_READ_CHARS:
                text = text[:_MAX_READ_CHARS] + f"\n\n… (앞 {_MAX_READ_CHARS}자만 보냈습니다)"
            return text
        if name == "find_nodes":
            nodes = docsearch.find_nodes(
                store.name, str(args.get("kind") or ""), str(args.get("q") or ""), _SCOPE_FETCH)
            return json.dumps([n for n in nodes if allowed(n["path"])][:_limit(args, 20, 50)],
                              ensure_ascii=False)
        if name == "neighbors":
            found = docsearch.neighbors(
                store.name, str(args.get("kind", "")), str(args.get("name", "")), _SCOPE_FETCH)
            for side in ("out", "in"):
                if isinstance(found.get(side), list):
                    found[side] = [e for e in found[side] if allowed(e["path"])]
            return json.dumps(found, ensure_ascii=False)
        raise mcp_server.McpToolError(f"알 수 없는 도구: {name}")

    return ("my", _PERSONAL_TOOLS, call)


def _recent(root, limit: int, allowed: Callable[[str], bool]) -> list[dict]:
    """새것부터. 메일은 받은 날짜가 파일 이름 앞이라 이름으로, 문서는 수정 시각으로 줄 세운다
    — 메일 파일의 수정 시각은 받은 때가 아니라 동기화한 때다."""
    items = []
    for path in root.rglob("*") if root.is_dir() else []:
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if not allowed(rel):
            continue
        if rel.startswith(f"{personal.MAIL_DIR}/"):
            key, title = path.name[:10], _first_heading(path)
        else:
            key, title = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d"), path.stem
        items.append({"path": rel, "date": key, "title": title})
    return sorted(items, key=lambda i: i["date"], reverse=True)[:limit]


def _first_heading(path) -> str:
    try:
        with path.open(encoding="utf-8") as f:
            first = f.readline()
    except OSError:
        return ""
    return first.removeprefix("# ").strip()


# --- 화면 도구 ---

def _view_tools(agent_names: list[str]) -> list[dict]:
    tools = [{
        "name": "suggest_tasks",
        "description": (
            "지금 진행할 업무를 제안 버튼으로 띄운다. 제안 하나 = 업무 단위 한 건(target)과 그"
            " 건에 적용할 부서 워크플로. 대상이 없는 제안은 버려진다. 사람이 버튼을 누르면"
            " prompt가 그대로 다음 요청이 된다 — prompt는 그 사람이 직접 쓴 요청처럼 대상을"
            " 이름으로 밝힌 완결된 문장으로 쓴다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "버튼에 쓸 짧은 제목"},
                            "prompt": {"type": "string"},
                            "target": {
                                "type": "object",
                                "description": "진행할 업무 단위 한 건",
                                "properties": {
                                    "kind": {"type": "string",
                                             "description": "종류 — 워크플로 targets의 kind(과제·계약·구매요청 등)"},
                                    "name": {"type": "string",
                                             "description": "그 건을 가리키는 이름(예: A사 유지보수 계약)"},
                                    "state": {"type": "string",
                                              "description": "지금 상태 — 워크플로 targets의 states 중(모르면 비움)"},
                                },
                                "required": ["kind", "name"],
                            },
                            "workflow": {"type": "string",
                                         "description": "근거가 된 부서 워크플로 이름(없으면 비움)"},
                            "why": {"type": "string", "description": "근거(어느 메일·문서) 한 줄"},
                        },
                        "required": ["title", "prompt", "target"],
                    },
                },
            },
            "required": ["tasks"],
        },
    }, {
        "name": "ask_user",
        "description": (
            "실행하기 전에 사람에게 확인받는다 — 요청이 모호하거나 그 일을 할 수 있는 도구·"
            "에이전트·출처가 둘 이상일 때. 선택지는 버튼으로 뜨고, 누르면 prompt가 그대로 다음"
            " 요청이 된다 — prompt는 그 선택을 확정한 완결된 요청으로 쓴다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string", "description": "버튼에 쓸 짧은 이름"},
                            "prompt": {"type": "string"},
                        },
                        "required": ["label", "prompt"],
                    },
                },
            },
            "required": ["question", "options"],
        },
    }, {
        "name": "show_report",
        "description": (
            "요청의 결과를 오른쪽 화면에 보고서로 띄운다. 결과는 대화에 쓰지 않고 늘 이것으로"
            " 낸다. content는 완결된 HTML 문서다(<style> 인라인 가능, 스크립트·외부 자원은"
            " 막힌다)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string", "description": "HTML 문서"},
            },
            "required": ["title", "content"],
        },
    }]
    if agent_names:
        tools.insert(0, {
            "name": "show_agent",
            "description": (
                "오른쪽 화면에 사내 에이전트(배포된 업무 앱)를 띄운다. 요청한 일을 맡는"
                " 에이전트가 하나면 **다른 무엇보다 먼저** 이것을 부른다(둘 이상이면 ask_user)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": agent_names},
                    "reason": {"type": "string", "description": "왜 이 에이전트인지 한 줄"},
                },
                "required": ["name"],
            },
        })
    return tools


def _target(raw) -> dict | None:
    if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
        return None
    return {"kind": str(raw.get("kind") or "")[:40], "name": str(raw["name"]).strip()[:120],
            "state": str(raw.get("state") or "")[:40]}


def _system_prompt(email: str, agent_list: list[dict], has_personal: bool,
                   workflow_names: list[str], task: dict | None = None,
                   participants: list[str] | None = None) -> str:
    lines = [
        "당신은 사내 업무 도우미 '스마트워크'다. 한국어로 답한다.",
        f"지금 말하는 사람: {email}",
    ]
    if participants and len(participants) > 1:
        lines.append(
            f"이 대화는 여러 사람이 함께 쓴다({', '.join(participants)}). 사용자 메시지 앞의"
            " [이메일]이 말한 사람이다. 답변은 참여자 모두에게 보인다.")
    if task:
        # 세션 = 업무 하나. 소유자가 고른 조직·워크플로가 이 대화의 주제다.
        lines += ["", f"이 대화의 업무: 조직 {task['org']}"
                  + (f" / 워크플로 {task['workflow']['name']}" if task.get("workflow") else "")]
        if task.get("workflow"):
            wf = task["workflow"]
            if wf.get("description"):
                lines.append(f"설명: {wf['description']}")
            lines += [f"- 단계: {step}" for step in wf.get("steps", [])]
            lines += [f"- 규칙: {rule}" for rule in wf.get("constraints", [])]
            lines.append("제안·답변은 이 워크플로의 업무 안에서 한다.")
    lines += [
        "",
        "대화와 보고서:",
        "- 대화는 요청을 정확히 확인하는 자리다. 요청의 결과는 대화에 쓰지 않고 늘"
        " show_report(HTML 보고서)로 낸다. 대화 답변은 확인 질문이나 보고서의 한두 줄 요지만 쓴다.",
        "- 요청이 모호하면(대상·범위·기간·결과 모양) 실행하기 전에 ask_user로 확인하고 멈춘다.",
        "- 그 일을 할 수 있는 도구·에이전트·출처가 둘 이상이면 고르지 말고 ask_user로 어느 것을"
        " 쓸지 확인받는다. 사람이 이미 고른 것은 다시 묻지 않는다.",
        "- 첨부 문서·이미지는 이번 요청의 참고 자료다.",
        "",
        "도구 사용 규칙:",
        "- 사내 문서·온톨로지·API·코드에 근거해 답한다. 추측하지 말고 도구로 확인한 뒤,"
        " 근거가 된 문서(저장소·경로)를 보고서에 밝힌다.",
        "- 요청한 일을 맡는 에이전트가 아래 목록에 하나 있으면 show_agent를 먼저 부른다.",
        "- 업무 제안(suggest_tasks)은 진행할 업무 단위 한 건(과제·계약·구매요청 등)마다 하나다."
        " 대상은 개인 업무 맥락에서 찾고(target), 그 건에 맞는 부서 워크플로(dept__workflows)를"
        " workflow에 적어 그 워크플로의 상태·전이로 다음 할 일을 정한다. 사람 단계에서 멈춘 실행이"
        " 있는 워크플로의 건을 먼저 다룬다. 대상을 찾지 못했으면 지어내지 말고 없다고 밝힌다.",
    ]
    if has_personal:
        lines.append(
            "- my__ 도구는 지금 말하는 사람만의 문서·메일 중 이 업무에 고른 것이다. 개인 업무"
            " 맥락이 필요한 질문(내 일정, 내가 받은 메일, 내 문서)에 쓴다. 사내 문서와 섞어"
            " 인용할 때는 어느 쪽인지 밝힌다.")
    else:
        lines.append("- 지금 말하는 사람은 이 업무에 개인 업무 맥락(로컬 문서·메일)을 고르지 않았다.")
    lines += ["", "부서 워크플로:"]
    lines += [f"- {name}" for name in workflow_names] or ["(없음 — 소속 조직에 워크플로가 없다)"]
    lines += ["", "에이전트 목록:"]
    lines += [f"- {a['name']} ({a['type']}, 조직 {a['org'] or '공용'})" for a in agent_list] \
        or ["(없음)"]
    return "\n".join(lines)


def _clean_history(history: list[dict], shared: bool = False) -> list[dict]:
    out = []
    for msg in history[-MAX_HISTORY:]:
        role = msg.get("role")
        content = str(msg.get("content") or "")[:MAX_MESSAGE_CHARS]
        for a in msg.get("attachments") or []:
            # 이미지는 그 턴에만 모델에게 갔다 — 뒤 턴에는 이름만 남는다.
            content += (f"\n\n[첨부 문서: {a['name']}]\n{a['text']}" if a.get("kind") == "document"
                        else f"\n\n[첨부 이미지: {a['name']}]")
        if shared and role == "user" and msg.get("author"):
            content = f"[{msg['author']}] {content}"
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    # 첫 턴(빈 대화)은 여는 지시로, 그 뒤로도 대화가 제안(assistant)으로 시작하면 그 지시를
    # 앞에 되살린다 — 대화가 assistant로 시작하면 거부하는 프로바이더가 있고(Bedrock),
    # 무엇에 대한 제안이었는지도 모델이 알아야 한다.
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": OPENING_PROMPT})
    return out


def chat(db: Session, key: ApiKey, provider, history: list[dict],
         toolsets: list[Toolset], *, org_id: int | None = None, workflow: str = "",
         participants: list[str] | None = None, folders: list[str] | None = None,
         mail: bool = False, images: list[str] | None = None) -> dict:
    """대화 한 턴. history의 마지막이 이번 사용자 메시지다 — 비어 있으면 여는 턴이다.

    세션 맥락: org_id·workflow(이름)는 소유자가 고른 업무, participants는 공유 참여자,
    folders·mail은 **지금 말하는 사람이** 이 세션에 고른 개인 맥락이다. images는 이번
    사용자 메시지에 붙인 이미지(data URL)다.

    돌려주는 것: 답변, 제안 업무, 오른쪽 화면에 띄울 에이전트·보고서(없을 수 있다 —
    그러면 화면은 대시보드를 그대로 둔다), 쓴 도구 이름.
    """
    agent_list = agents(db, key)
    by_name = {a["name"]: a for a in agent_list}
    mine = personal_toolset(db, key.name, folders, mail)
    dept = department_workflows(db, key, org_id)
    task = None
    if org_id is not None:
        org = db.get(Organization, org_id)
        task = {"org": org.name if org else "",
                "workflow": next((w for w in dept if w["name"] == workflow), None)}
    dept_set: Toolset = ("dept", _DEPT_TOOLS,
                         lambda n, a: json.dumps(dept, ensure_ascii=False))
    sets = [("view", _view_tools(list(by_name)), None), dept_set, *toolsets,
            *([mine] if mine else [])]

    tools: list[dict] = []
    registry: dict[str, tuple[Callable | None, str]] = {}
    for prefix, mcp_tools, call in sets:
        for t in mcp_tools:
            # 화면 도구는 접두사 없이 — 모델이 시스템 프롬프트의 이름 그대로 부른다.
            fn_name = t["name"] if prefix == "view" else f"{prefix}__{t['name']}"
            tools.append({"type": "function", "function": {
                "name": fn_name, "description": t.get("description", ""),
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
            }})
            registry[fn_name] = (call, t["name"])

    used: list[str] = []
    view: dict = {"agent": None, "report": None, "suggestions": [], "choices": []}
    names = {w["name"] for w in dept}

    def execute(fn_name: str, args: dict) -> str:
        used.append(fn_name)
        if fn_name == "show_agent":
            found = by_name.get(str(args.get("name", "")))
            if found is None:
                return "그런 에이전트가 없습니다 — 목록의 이름을 쓰세요."
            view["agent"] = {**found, "reason": str(args.get("reason") or "")}
            return f"{found['name']}을 오른쪽 화면에 띄웠습니다."
        if fn_name == "suggest_tasks":
            tasks = args.get("tasks") if isinstance(args.get("tasks"), list) else []
            view["suggestions"] = [
                {"title": str(t.get("title") or "")[:80], "prompt": str(t.get("prompt") or ""),
                 "target": _target(t.get("target")),
                 # 부서에 없는 이름은 버린다 — 화면이 "워크플로: …"로 근거를 내보이는 자리다.
                 "workflow": str(t.get("workflow") or "") if t.get("workflow") in names else "",
                 "why": str(t.get("why") or "")}
                for t in tasks if isinstance(t, dict) and t.get("title") and t.get("prompt")
                # 대상 없는 제안은 버린다 — 워크플로만으로는 진행할 일이 아니다.
                and _target(t.get("target"))
            ][:MAX_SUGGESTIONS]
            return f"제안 {len(view['suggestions'])}건을 띄웠습니다."
        if fn_name == "ask_user":
            options = args.get("options") if isinstance(args.get("options"), list) else []
            view["choices"] = [
                {"label": str(o.get("label"))[:80], "prompt": str(o.get("prompt"))}
                for o in options if isinstance(o, dict) and o.get("label") and o.get("prompt")
            ][:MAX_CHOICES]
            return f"선택지 {len(view['choices'])}개를 띄웠습니다. 사람이 고를 때까지 멈춘다."
        if fn_name == "show_report":
            view["report"] = {
                "title": str(args.get("title") or "보고서"),
                "format": "html",
                "content": str(args.get("content") or ""),
            }
            return "보고서를 오른쪽 화면에 띄웠습니다."
        entry = registry.get(fn_name)
        if entry is None or entry[0] is None:
            return f"unknown tool: {fn_name}"
        call, tool_name = entry
        try:
            return call(tool_name, args)
        except mcp_server.McpToolError as e:
            return f"도구 오류: {e}"
        except Exception as e:  # noqa: BLE001 — 도구 하나가 실패해도 대화는 이어진다
            return f"tool call failed: {e}"

    shared = len(participants or []) > 1
    messages = [{"role": "system", "content": _system_prompt(
                    key.name, agent_list, mine is not None, [w["name"] for w in dept],
                    task, participants)},
                *_clean_history(history, shared)]
    if images and messages[-1]["role"] == "user":
        messages[-1] = {"role": "user", "content": [
            {"type": "text", "text": messages[-1]["content"]},
            *({"type": "image_url", "image_url": {"url": url}} for url in images)]}
    reply = llm_service.chat_completion(provider, messages, db, tools, execute)
    # 무엇을 물었는지는 남기지 않는다 — 개인 메일·문서 내용이 감사 기록으로 새면 안 된다.
    audit.record(db, key.name, "smartwork.chat", "-", {"tools": used})
    return {"reply": reply, **view, "tools": used, "provider": provider.name}
