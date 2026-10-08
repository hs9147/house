"""스마트워크 — 대화 한 턴을 도구와 함께 돌리고, 오른쪽 화면에 무엇을 띄울지 정한다.

화면은 모델이 **도구로** 고른다(show_agent·show_report). 답변 본문에서 "에이전트를
열어 드릴게요" 같은 문구를 찾아 화면을 바꾸면 말투가 조금만 달라져도 어긋난다 — 도구
호출은 이름과 인자가 정해진 신호라 화면이 그것만 따르면 된다.

사내 MCP 도구(문서·온톨로지·API·코드·운영)는 api/smartwork.py가 골라 넘긴다. 이 모듈은
그것들을 하나의 도구 목록으로 묶고, 그 사람만의 개인 도구(services/personal.py의
저장소)를 **그 사람의 이메일에 묶어** 붙인다 — 개인 도구에는 누구의 저장소인지 고르는
인자가 없다.
"""
import json
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..models import (
    ApiKey, BuildProfile, Deployment, DeploymentStatus, Project, Workflow, WorkflowRun,
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
REPORT_FORMATS = ("md", "html", "csv")
MAX_SUGGESTIONS = 6
# 대화는 **제안으로 시작한다** — 빈 화면에 "무엇을 도와드릴까요"를 띄우면 사람이 할 일을
# 떠올려야 한다. 첫 턴에는 이 지시를 사용자 차례로 넣어 모델이 업무 맥락을 먼저 훑게 한다.
# 화면에는 나오지 않는다(콘솔은 답변부터 보여 준다).
OPENING_PROMPT = (
    "대화를 시작한다. dept__workflows로 우리 부서 워크플로를 먼저 보고, 그 업무 중에서"
    " 지금 진행할 만한 것을 제안해 줘. 개인 업무 맥락이 있으면 my__recent로 최근 메일·문서를"
    " 보고 어느 업무가 지금 진행 중인지 판단하는 근거로 삼고, 맡을 에이전트가 있는 업무는"
    " 그렇다고 밝혀 줘. 제안은 suggest_tasks로 띄우고 대화에는 인사와 한두 줄 요약만 써."
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

def department_workflows(db: Session, key: ApiKey) -> list[dict]:
    """이 사람의 **부서**(소속 조직) 워크플로 — 업무 제안의 근거.

    제안을 모델의 짐작에 맡기면 "그럴듯한데 우리 부서 일이 아닌" 일이 나온다. 부서가 실제로
    하는 일의 목록은 워크플로가 이미 갖고 있다(단계·사람 작업·제약). 관리자라도 소속이 없으면
    부서가 없다 — 모든 조직의 워크플로를 섞으면 남의 부서 일을 제안한다.
    """
    org_ids = viewer_org_ids(db, key)
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
        })
    return out


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
        " 단계에서 멈춰 있는 실행 수. 업무를 제안할 때 먼저 부른다."
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


def personal_toolset(db: Session, email: str) -> Toolset | None:
    """동의한 사람에게만 붙는다. 저장소는 여기서 이메일로 정해지고 인자로 바뀌지 않는다."""
    if personal.get(db, email) is None:
        return None
    store = personal.store_for(email)

    def call(name: str, args: dict) -> str:
        if name == "sources":
            return json.dumps(personal.status(db, email), ensure_ascii=False, default=str)
        if name == "recent":
            return json.dumps(_recent(store.root, _limit(args, 20, 50)), ensure_ascii=False)
        if name == "search":
            found = docsearch.search(store.name, str(args.get("query", "")), _limit(args, 10, 30))
            return json.dumps(found, ensure_ascii=False)
        if name == "read":
            try:
                target = storage_service.resolve(store.root, str(args.get("path", "")))
                rel = target.relative_to(store.root).as_posix()
                if not target.is_file():
                    raise mcp_server.McpToolError(f"파일이 없습니다: {rel}")
                text = docready.read(store.name, rel, target)
            except (storage_service.StorageError, doctext.ExtractError) as e:
                raise mcp_server.McpToolError(str(e))
            if len(text) > _MAX_READ_CHARS:
                text = text[:_MAX_READ_CHARS] + f"\n\n… (앞 {_MAX_READ_CHARS}자만 보냈습니다)"
            return text
        if name == "find_nodes":
            return json.dumps(docsearch.find_nodes(
                store.name, str(args.get("kind") or ""), str(args.get("q") or ""),
                _limit(args, 20, 50)), ensure_ascii=False)
        if name == "neighbors":
            return json.dumps(docsearch.neighbors(
                store.name, str(args.get("kind", "")), str(args.get("name", ""))),
                ensure_ascii=False)
        raise mcp_server.McpToolError(f"알 수 없는 도구: {name}")

    return ("my", _PERSONAL_TOOLS, call)


def _recent(root, limit: int) -> list[dict]:
    """새것부터. 메일은 받은 날짜가 파일 이름 앞이라 이름으로, 문서는 수정 시각으로 줄 세운다
    — 메일 파일의 수정 시각은 받은 때가 아니라 동기화한 때다."""
    items = []
    for path in root.rglob("*") if root.is_dir() else []:
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
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
            "지금 진행할 만한 업무를 제안 버튼으로 띄운다. 업무는 부서 워크플로에서 고른다."
            " 사람이 버튼을 누르면 prompt가 그대로 다음 요청이 된다 — prompt는 그 사람이 직접"
            " 쓴 요청처럼 완결된 문장으로 쓴다."
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
                            "workflow": {"type": "string",
                                         "description": "근거가 된 부서 워크플로 이름(없으면 비움)"},
                            "why": {"type": "string", "description": "근거(어느 메일·문서) 한 줄"},
                        },
                        "required": ["title", "prompt"],
                    },
                },
            },
            "required": ["tasks"],
        },
    }, {
        "name": "show_report",
        "description": (
            "오른쪽 화면에 보고서를 띄운다. 표·목록·여러 단락으로 정리할 결과는 대화에 길게"
            " 쓰지 말고 이것으로 띄운 뒤 대화에는 요지만 쓴다. format: md(마크다운) |"
            " html(완결된 HTML 문서, 스크립트는 실행되지 않는다) | csv(첫 줄이 머리글)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "format": {"type": "string", "enum": list(REPORT_FORMATS)},
                "content": {"type": "string"},
            },
            "required": ["title", "format", "content"],
        },
    }]
    if agent_names:
        tools.insert(0, {
            "name": "show_agent",
            "description": (
                "오른쪽 화면에 사내 에이전트(배포된 업무 앱)를 띄운다. 요청한 일을 맡는"
                " 에이전트가 있으면 **다른 무엇보다 먼저** 이것을 부른다."
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


def _system_prompt(email: str, agent_list: list[dict], has_personal: bool,
                   workflow_names: list[str]) -> str:
    lines = [
        "당신은 사내 업무 도우미 '스마트워크'다. 한국어로 답한다.",
        f"대화 상대: {email}",
        "",
        "도구 사용 규칙:",
        "- 사내 문서·온톨로지·API·코드에 근거해 답한다. 추측하지 말고 도구로 확인한 뒤,"
        " 근거가 된 문서(저장소·경로)를 밝힌다.",
        "- 요청한 일을 맡는 에이전트가 아래 목록에 있으면 show_agent를 먼저 부른다.",
        "- 정리된 결과(표·목록·여러 단락)는 show_report로 오른쪽 화면에 띄우고, 대화에는"
        " 요지만 짧게 쓴다.",
        "- 업무 제안(suggest_tasks)은 부서 워크플로(dept__workflows)를 근거로 한다. 제안마다"
        " 어느 워크플로의 업무인지 workflow에 적고, 사람 단계에서 멈춘 실행이 있는 워크플로를"
        " 먼저 다룬다. 부서 워크플로가 없으면 없다고 밝힌 뒤 다른 맥락으로 제안한다.",
    ]
    if has_personal:
        lines.append(
            "- my__ 도구는 이 사람만의 문서·메일이다. 개인 업무 맥락이 필요한 질문(내 일정,"
            " 내가 받은 메일, 내 문서)에 쓴다. 사내 문서와 섞어 인용할 때는 어느 쪽인지 밝힌다.")
    else:
        lines.append("- 이 사람은 개인 업무 맥락(로컬 문서·메일)을 연결하지 않았다.")
    lines += ["", "부서 워크플로:"]
    lines += [f"- {name}" for name in workflow_names] or ["(없음 — 소속 조직에 워크플로가 없다)"]
    lines += ["", "에이전트 목록:"]
    lines += [f"- {a['name']} ({a['type']}, 조직 {a['org'] or '공용'})" for a in agent_list] \
        or ["(없음)"]
    return "\n".join(lines)


def _clean_history(history: list[dict]) -> list[dict]:
    out = []
    for msg in history[-MAX_HISTORY:]:
        role = msg.get("role")
        content = str(msg.get("content") or "")[:MAX_MESSAGE_CHARS]
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    # 첫 턴(빈 대화)은 여는 지시로, 그 뒤로도 대화가 제안(assistant)으로 시작하면 그 지시를
    # 앞에 되살린다 — 대화가 assistant로 시작하면 거부하는 프로바이더가 있고(Bedrock),
    # 무엇에 대한 제안이었는지도 모델이 알아야 한다.
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": OPENING_PROMPT})
    return out


def chat(db: Session, key: ApiKey, provider, history: list[dict],
         toolsets: list[Toolset]) -> dict:
    """대화 한 턴. history의 마지막이 이번 사용자 메시지다 — 비어 있으면 여는 턴이다.

    돌려주는 것: 답변, 제안 업무, 오른쪽 화면에 띄울 에이전트·보고서(없을 수 있다 —
    그러면 화면은 대시보드를 그대로 둔다), 쓴 도구 이름.
    """
    agent_list = agents(db, key)
    by_name = {a["name"]: a for a in agent_list}
    mine = personal_toolset(db, key.name)
    dept = department_workflows(db, key)
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
    view: dict = {"agent": None, "report": None, "suggestions": []}
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
                 # 부서에 없는 이름은 버린다 — 화면이 "워크플로: …"로 근거를 내보이는 자리다.
                 "workflow": str(t.get("workflow") or "") if t.get("workflow") in names else "",
                 "why": str(t.get("why") or "")}
                for t in tasks if isinstance(t, dict) and t.get("title") and t.get("prompt")
            ][:MAX_SUGGESTIONS]
            return f"제안 {len(view['suggestions'])}건을 띄웠습니다."
        if fn_name == "show_report":
            fmt = str(args.get("format", "md"))
            view["report"] = {
                "title": str(args.get("title") or "보고서"),
                "format": fmt if fmt in REPORT_FORMATS else "md",
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

    messages = [{"role": "system", "content": _system_prompt(key.name, agent_list, mine is not None,
                                                         [w["name"] for w in dept])},
                *_clean_history(history)]
    reply = llm_service.chat_completion(provider, messages, db, tools, execute)
    # 무엇을 물었는지는 남기지 않는다 — 개인 메일·문서 내용이 감사 기록으로 새면 안 된다.
    audit.record(db, key.name, "smartwork.chat", "-", {"tools": used})
    return {"reply": reply, **view, "tools": used, "provider": provider.name}
