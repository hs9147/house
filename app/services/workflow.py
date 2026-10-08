"""워크플로 — 조직이 플랫폼 자원을 엮어 **실제로 돌리는** 작은 그래프.

표현은 작은 JSON 스펙이다. BPMN 2.0도 Petri net도 쓰지 않았다: 요소의 대부분은 끝내 쓰이지
않고(zur Muehlen & Recker 2008 — 실제 모델은 어휘의 일부만 쓴다), 우리 경로에서는 **LLM이
만들고 고쳐야** 하는데 XML은 검증·수선 비용이 크다. 워크플로 자동 생성 연구가 코드/구조화
표현을 택하는 이유가 같다(AFlow 2024: 코드로 표현된 워크플로의 탐색, ProMoAI 2024: 생성 →
오류 처리 → 사람이 대화로 수정).

**노드 출력 규약은 하나다** — 모든 노드가 `{"text": str, "paths": [str]}`을 낸다. 종류마다
다른 모양을 내면 "무엇을 무엇에 꽂을 수 있나"가 종류의 곱이 되고, 그걸 사람도 LLM도 외우지
못한다. 입력은 들어오는 엣지의 출처 출력을 순서대로 이어 붙인 것이다.

**사람 단계가 일급이다.** 사람이 하는 일(검토·승인·입력)이 흐름 안에 있어야 사내 업무가
그려진다. 그 노드에 닿으면 실행은 waiting으로 멈추고, 앞 단계 출력을 행에 남긴다 — 몇 시간
뒤에 이어질 수 있고, 그때 출력이 없으면 LLM 단계를 처음부터 다시 돌려야 한다(돈과 시간이다).

사람 단계는 **흐름 전체**를 멈춘다(병렬 경로도 함께 기다린다). 업무에서 승인은 "그 결정이
날 때까지 기다린다"는 뜻이고, 결정을 기다리는 동안 다른 쪽이 진행해 파일을 쓰면 되돌릴 수
없는 일이 승인 전에 일어난다. 동시 진행이 필요해지면 그때 경로별 대기로 바꾼다.
"""
import json
import re
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    LlmProvider, Module, Organization, Workflow, WorkflowRun, WorkflowRunStatus,
)
from ..models import utcnow
from . import docready, docsearch, llm, mcp_client, storage
from . import modules as modules_service

# 상한. 사람이 읽을 수 있는 크기를 넘으면 그림도 실행 기록도 쓸모가 없어진다.
MAX_NODES = 30
MAX_TEXT = 20_000          # 노드 출력 텍스트 저장 상한
MAX_PATHS = 200            # 파일 목록 상한
RUN_BUDGET_SECONDS = 900.0  # 실행 한 번의 시간 예산(LLM 단계가 느리다)
_SUMMARY = 160             # 단계 요약 표시 길이

# 노드 종류 — **플랫폼이 이미 가진 자원**만 있다. 여기 없는 일은 워크플로가 할 수 없고,
# 그 사실이 화면과 LLM 프롬프트에 같은 목록으로 간다(없는 도구를 지어내지 못하게).
NODE_TYPES: dict[str, dict] = {
    "storage.list": {
        "label": "파일 목록",
        "required": ("store",),
        "optional": ("prefix", "suffix", "limit"),
        "help": "저장소의 파일 경로를 모은다. prefix·suffix로 좁힌다.",
    },
    "docs.search": {
        "label": "문서 검색",
        "required": ("store", "query"),
        "optional": ("limit",),
        "help": "색인에서 낱말을 모두 포함하는 문서를 찾아 발췌와 경로를 낸다.",
    },
    "graph.find": {
        "label": "온톨로지 찾기",
        "required": ("store",),
        "optional": ("kind", "q", "limit"),
        "help": "색인이 뽑아 둔 그래프에서 노드를 찾는다(kind=document·section·term·table)."
                " 찾은 노드가 있는 **문서 경로**를 함께 내므로 다음 단계에서 바로 읽을 수 있다.",
    },
    "doc.read": {
        "label": "문서 읽기",
        "required": (),
        "optional": ("store", "path", "limit"),
        "help": "앞 단계가 준 경로(또는 path)의 본문을 마크다운으로 읽는다.",
    },
    "llm": {
        "label": "LLM",
        "required": ("prompt",),
        "optional": ("provider",),
        "help": "입력을 컨텍스트로 프롬프트를 실행한다. provider를 비우면 기본 프로바이더.",
    },
    "mcp.tool": {
        "label": "모듈 도구",
        "required": ("module", "tool"),
        "optional": ("args",),
        "help": "조직에 보이는 MCP 모듈의 도구를 호출한다.",
    },
    "branch": {
        "label": "분기",
        "required": ("when",),
        "optional": (),
        "help": "참/거짓으로 갈린다. when.kind는 contains·empty·llm 중 하나.",
    },
    "human": {
        "label": "사람 작업",
        "required": ("title",),
        "optional": ("role", "instruction"),
        "help": "사람이 할 일. 실행이 여기서 멈추고, 사람이 제출한 내용이 이 노드의 출력이 된다.",
    },
    "storage.write": {
        "label": "파일 저장",
        "required": ("store", "path"),
        "optional": (),
        "help": "입력 텍스트를 저장소에 파일로 쓴다(읽기 전용 저장소는 쓸 수 없다).",
    },
}

BRANCH_KINDS = ("contains", "empty", "llm")
# 온톨로지 노드 종류(services/ontology.py가 만드는 것) — 여기 없는 kind를 적으면 조용히
# 0건이 나온다. 0건과 "그런 종류가 없다"는 다른 사실이므로 검증에서 가른다.
GRAPH_KINDS = ("document", "section", "term", "table")
CASES = ("참", "거짓")

_ID_RE = re.compile(r"^[^\s/\\]{1,64}$")


class WorkflowError(RuntimeError):
    """실행을 더 진행할 수 없다 — 이유는 사람이 읽는 문장으로."""


# --- 자원 목록 (화면과 LLM 프롬프트가 같은 목록을 쓴다) ---

def constraints_of(workflow: Workflow) -> list[str]:
    """**이 워크플로의** 제약사항 — 대화에서 읽어 내 함께 저장한 것(extracted.constraints).

    조직 단위 등록부를 뒀다가 걷었다. 업무 규칙은 흐름마다 다르다 — 계약 검토의 선급금 한도는
    신규업체등록 워크플로가 지킬 규칙이 아니다. 조직에 묶어 두면 모든 워크플로가 남의 규칙을
    받고, 평가 화면에 "이 규칙을 지키는 단계가 없습니다"가 엉뚱한 데서 뜬다(기획의 개발
    제약을 섞었을 때 겪은 것과 같은 일이 한 단계 작은 규모로 되풀이된다).

    기획의 공통 제약사항(planning.common_constraints)과도 섞지 않는다: 그쪽은 에이전트를
    **개발**할 때의 제한이다.
    """
    rows = (workflow.extracted or {}).get("constraints")
    if not isinstance(rows, list):
        return []
    return [str(r.get("text")).strip() for r in rows
            if isinstance(r, dict) and str(r.get("text") or "").strip()]


def resources(db: Session, organization_id: int) -> dict:
    """이 조직이 워크플로에서 **쓸 수 있는 것** 전부.

    화면의 선택지와 LLM 프롬프트의 사실이 같은 함수에서 나와야 한다 — 다르면 LLM이 고른
    것을 사람이 저장할 수 없거나, 그 반대가 된다.
    """
    org = db.get(Organization, organization_id)
    mods = db.execute(
        select(Module).where(
            (Module.organization_id.is_(None)) | (Module.organization_id == organization_id)
        ).order_by(Module.name)
    ).scalars().all()
    providers = db.execute(
        select(LlmProvider).where(
            (LlmProvider.organization_id.is_(None))
            | (LlmProvider.organization_id == organization_id)
        ).order_by(LlmProvider.name)
    ).scalars().all()
    default = llm.default_provider(db)
    return {
        "organization": org.name if org else "",
        "stores": [
            {"name": s.name, "read_only": s.read_only, "exists": s.root.is_dir()}
            for s in storage.visible_stores(db) if s.organization_id == organization_id
        ],
        "modules": [
            {"name": m.name, "type": m.type.value, "category": m.category or ""}
            for m in mods
        ],
        "providers": [
            {"name": p.name, "model": p.model, "is_default": bool(p.is_default)}
            for p in providers
        ],
        "default_provider": default.name if default else "",
        "node_types": [
            {"type": key, "label": spec["label"], "help": spec["help"],
             "required": list(spec["required"]), "optional": list(spec["optional"])}
            for key, spec in NODE_TYPES.items()
        ],
        "branch_kinds": list(BRANCH_KINDS),
        "cases": list(CASES),
    }


# --- 검증 ---

def validate(db: Session, organization_id: int, spec: dict) -> list[str]:
    """스펙의 문제를 **전부** 모아 돌려준다(첫 번째에서 멈추지 않는다).

    저장을 막는 용도다. 기동 스크립트와 같은 자리에 선다 — 검증을 통과하지 못한 것을 경고만
    하고 넘기면, 틀린 것이 저장되고 그 뒤에 실행이 엉뚱한 데서 깨진다. LLM이 만든 스펙은
    이 목록을 그대로 받아 한 번 고쳐 본다(workflowchat).
    """
    problems: list[str] = []
    if not isinstance(spec, dict):
        return ["스펙이 객체(JSON)가 아닙니다."]
    nodes = spec.get("nodes")
    edges = spec.get("edges", [])
    if not isinstance(nodes, list) or not nodes:
        return ["nodes가 비어 있습니다 — 단계가 하나는 있어야 합니다."]
    if not isinstance(edges, list):
        return ["edges가 배열이 아닙니다."]
    if len(nodes) > MAX_NODES:
        problems.append(f"단계가 {len(nodes)}개입니다 — 상한은 {MAX_NODES}개입니다.")

    res = resources(db, organization_id)
    store_by_name = {s["name"]: s for s in res["stores"]}
    module_names = {m["name"] for m in res["modules"]}
    provider_names = {p["name"] for p in res["providers"]}

    seen: set[str] = set()
    by_id: dict[str, dict] = {}
    for index, node in enumerate(nodes):
        where = f"{index + 1}번째 단계"
        if not isinstance(node, dict):
            problems.append(f"{where}가 객체가 아닙니다.")
            continue
        node_id = str(node.get("id") or "")
        if not _ID_RE.match(node_id):
            problems.append(f"{where}의 id가 비었거나 공백·슬래시를 포함합니다: {node_id!r}")
            continue
        if node_id in seen:
            problems.append(f"id가 중복됩니다: {node_id}")
            continue
        seen.add(node_id)
        by_id[node_id] = node

        kind = str(node.get("type") or "")
        shape = NODE_TYPES.get(kind)
        if shape is None:
            problems.append(
                f"{node_id}: 모르는 종류 '{kind}' — 쓸 수 있는 것은 "
                f"{', '.join(NODE_TYPES)}입니다.")
            continue
        for field in shape["required"]:
            if not node.get(field):
                problems.append(f"{node_id}({shape['label']}): {field}가 필요합니다.")
        allowed = {"id", "type", "label", *shape["required"], *shape["optional"]}
        for field in node:
            if field not in allowed:
                # 조용히 무시하지 않는다 — 무시하면 "적었는데 안 듣는" 설정이 생긴다.
                problems.append(f"{node_id}: 쓰이지 않는 항목 '{field}'")

        store_name = node.get("store")
        if store_name:
            target = store_by_name.get(str(store_name))
            if target is None:
                problems.append(
                    f"{node_id}: 저장소 '{store_name}'가 없습니다 — "
                    f"{', '.join(store_by_name) or '등록된 저장소가 없습니다'}")
            elif kind == "storage.write" and target["read_only"]:
                problems.append(f"{node_id}: '{store_name}'는 읽기 전용 저장소입니다.")
        if kind == "graph.find" and node.get("kind"):
            if str(node["kind"]) not in GRAPH_KINDS:
                problems.append(
                    f"{node_id}: 모르는 노드 종류 '{node['kind']}' — "
                    f"{', '.join(GRAPH_KINDS)} 중 하나입니다.")
        if kind == "mcp.tool":
            if node.get("module") and str(node["module"]) not in module_names:
                problems.append(
                    f"{node_id}: 모듈 '{node['module']}'를 이 조직에서 쓸 수 없습니다.")
            if node.get("args") is not None and not isinstance(node["args"], dict):
                problems.append(f"{node_id}: args는 객체여야 합니다.")
        if kind == "llm" and node.get("provider"):
            if str(node["provider"]) not in provider_names:
                problems.append(
                    f"{node_id}: LLM 프로바이더 '{node['provider']}'를 쓸 수 없습니다.")
        if kind == "llm" and not node.get("provider") and not res["default_provider"]:
            problems.append(
                f"{node_id}: 기본 LLM 프로바이더가 지정되지 않았습니다 — "
                "LLM 관리에서 「설정」을 누르거나 provider를 적으세요.")
        if kind == "branch":
            when = node.get("when")
            if isinstance(when, dict):
                when_kind = str(when.get("kind") or "")
                if when_kind not in BRANCH_KINDS:
                    problems.append(
                        f"{node_id}: when.kind는 {', '.join(BRANCH_KINDS)} 중 하나입니다.")
                elif when_kind == "contains" and not when.get("text"):
                    problems.append(f"{node_id}: when.text가 필요합니다(contains).")
                elif when_kind == "llm" and not when.get("question"):
                    problems.append(f"{node_id}: when.question이 필요합니다(llm).")
            elif when is not None:
                problems.append(f"{node_id}: when은 객체여야 합니다(kind·text/question).")

    for index, edge in enumerate(edges):
        where = f"{index + 1}번째 연결"
        if not isinstance(edge, dict):
            problems.append(f"{where}가 객체가 아닙니다.")
            continue
        src, dst = str(edge.get("from") or ""), str(edge.get("to") or "")
        if src not in by_id:
            problems.append(f"{where}: 없는 단계에서 나갑니다: {src!r}")
        if dst not in by_id:
            problems.append(f"{where}: 없는 단계로 들어갑니다: {dst!r}")
        if src and src == dst:
            problems.append(f"{where}: 자기 자신으로 잇습니다: {src}")
        case = edge.get("case")
        if case is not None:
            if case not in CASES:
                problems.append(f"{where}: case는 {' 또는 '.join(CASES)}입니다: {case!r}")
            elif by_id.get(src, {}).get("type") != "branch":
                problems.append(f"{where}: case는 분기 단계에서 나가는 연결에만 둡니다.")

    problems.extend(_graph_problems(by_id, edges))
    return problems


def _graph_problems(by_id: dict[str, dict], edges: list) -> list[str]:
    """사이클·고립 — 실행 순서를 정할 수 없거나, 그려 놓고 돌지 않는 단계."""
    problems: list[str] = []
    valid = [e for e in edges
             if isinstance(e, dict) and str(e.get("from")) in by_id
             and str(e.get("to")) in by_id]
    order = _topological(by_id, valid)
    if order is None:
        problems.append("연결이 고리를 이룹니다 — 실행 순서를 정할 수 없습니다.")
    if len(by_id) > 1:
        linked = {str(e["from"]) for e in valid} | {str(e["to"]) for e in valid}
        for node_id in by_id:
            if node_id not in linked:
                problems.append(f"{node_id}: 아무 단계와도 이어지지 않았습니다.")
    for node_id, node in by_id.items():
        if node.get("type") != "branch":
            continue
        cases = {e.get("case") for e in valid if str(e["from"]) == node_id}
        if not cases & set(CASES):
            problems.append(
                f"{node_id}: 분기인데 참/거짓 연결이 없습니다 — 어느 쪽으로도 가지 않습니다.")
    return problems


def _topological(by_id: dict[str, dict], edges: list) -> list[str] | None:
    """실행 순서. 고리가 있으면 None — 검증이 그걸 문제로 말한다."""
    incoming = {node_id: 0 for node_id in by_id}
    after: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    for edge in edges:
        src, dst = str(edge["from"]), str(edge["to"])
        after[src].append(dst)
        incoming[dst] += 1
    # 스펙에 적힌 순서를 유지한다 — 같은 랭크의 단계가 매번 다른 순서로 돌면 기록을
    # 비교할 수 없다.
    ready = [node_id for node_id in by_id if incoming[node_id] == 0]
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for nxt in after[current]:
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
    return order if len(order) == len(by_id) else None


# --- 실행 ---

def start(db: Session, workflow: Workflow, actor: str) -> WorkflowRun:
    """실행을 시작한다 — 레코드를 먼저 만들고 작업 큐에서 돈다(배포와 같은 패턴).

    요청 안에서 끝내지 않는 이유: LLM 단계 하나가 분 단위다. 기다리는 쪽은 프록시 시간
    제한에 먼저 걸리고, 그러면 실행은 계속 도는데 화면은 실패로 본다.
    """
    problems = validate(db, workflow.organization_id, workflow.spec or {})
    if problems:
        raise WorkflowError("스펙에 문제가 있어 실행할 수 없습니다: " + " / ".join(problems[:3]))

    run = WorkflowRun(
        workflow_id=workflow.id, version=workflow.version,
        status=WorkflowRunStatus.running, actor=actor, steps=[], outputs={},
    )
    db.add(run)
    db.commit()
    _submit(run.id)
    return run


def resume(db: Session, run: WorkflowRun, content: str, approved: bool = True) -> WorkflowRun:
    """사람 단계에 제출한다 — 그 자리에서 이어 돈다.

    반려(approved=False)도 출력이다(사유가 다음 단계로 간다) — 다만 실행은 거기서 끝난다.
    업무에서 반려는 "흐름을 멈추는 결정"이고, 그 결정과 사유가 기록에 남는 것이 목적이다.
    """
    if run.status != WorkflowRunStatus.waiting or not run.pending_node:
        raise WorkflowError("이 실행은 사람 단계에서 기다리고 있지 않습니다.")
    node_id = run.pending_node
    outputs = dict(run.outputs or {})
    outputs[node_id] = {"text": content[:MAX_TEXT], "paths": []}
    run.outputs = outputs
    run.steps = [*(run.steps or []), {
        "id": node_id, "type": "human", "status": "ok" if approved else "rejected",
        "summary": (content or "")[:_SUMMARY], "ms": 0,
    }]
    run.pending_node = ""
    if not approved:
        run.status = WorkflowRunStatus.canceled
        run.error = f"{node_id}에서 반려되었습니다: {content[:200]}"
        run.finished_at = utcnow()
        db.commit()
        return run
    run.status = WorkflowRunStatus.running
    db.commit()
    _submit(run.id)
    return run


def _submit(run_id: int) -> None:
    from . import jobs  # noqa: PLC0415 — 작업 큐(배포와 공용)

    def _task() -> None:
        from ..db import SessionLocal  # noqa: PLC0415

        with SessionLocal() as session:
            run = session.get(WorkflowRun, run_id)
            if run is None:
                return
            try:
                _execute(session, run)
            except Exception as e:  # noqa: BLE001 — 실패는 레코드에 남긴다
                run.status = WorkflowRunStatus.failed
                run.error = f"{type(e).__name__}: {e}"[:2000]
                run.finished_at = utcnow()
                session.commit()

    jobs.submit(_task)


def _execute(db: Session, run: WorkflowRun) -> None:
    """예산 안에서 위상 순서대로 돈다. 사람 단계를 만나면 멈추고 waiting으로 남는다."""
    workflow = db.get(Workflow, run.workflow_id)
    if workflow is None:
        raise WorkflowError("워크플로가 사라졌습니다.")
    spec = workflow.spec or {}
    by_id = {str(n["id"]): n for n in spec.get("nodes", []) if isinstance(n, dict)}
    edges = [e for e in spec.get("edges", [])
             if isinstance(e, dict) and str(e.get("from")) in by_id
             and str(e.get("to")) in by_id]
    order = _topological(by_id, edges)
    if order is None:
        raise WorkflowError("연결이 고리를 이룹니다 — 실행 순서를 정할 수 없습니다.")

    outputs: dict[str, dict] = dict(run.outputs or {})
    steps: list[dict] = list(run.steps or [])
    done = {s["id"] for s in steps if s.get("status") in ("ok", "rejected")}
    skipped = {s["id"] for s in steps if s.get("status") == "skipped"}
    started = time.monotonic()

    for node_id in order:
        if node_id in done or node_id in skipped:
            continue
        node = by_id[node_id]
        kind = str(node.get("type"))
        incoming = [e for e in edges if str(e["to"]) == node_id]
        # 앞 단계가 건너뛰어졌거나 분기에서 다른 쪽이 선택됐으면 이 단계도 돌지 않는다.
        if incoming and not any(_edge_live(e, outputs, skipped) for e in incoming):
            skipped.add(node_id)
            steps.append({"id": node_id, "type": kind, "status": "skipped",
                          "summary": "앞 단계가 이 경로를 고르지 않았습니다.", "ms": 0})
            # steps는 run.steps의 **복사본**이다(JSON 컬럼은 재대입해야 바뀐 걸 안다) —
            # 여기서 대입하지 않으면 건너뛴 단계가 기록에서 조용히 사라진다.
            run.steps = steps
            db.commit()
            continue
        if time.monotonic() - started > RUN_BUDGET_SECONDS:
            run.steps = steps
            run.outputs = outputs
            run.status = WorkflowRunStatus.failed
            run.error = (f"실행 예산 {int(RUN_BUDGET_SECONDS)}초를 넘겼습니다 — "
                         f"{node_id} 앞에서 멈췄습니다. 단계를 줄이거나 범위를 좁히세요.")
            run.finished_at = utcnow()
            db.commit()
            return

        if kind == "human":
            # 여기서 멈춘다. 앞 단계 출력을 남겨 두는 것이 이 설계의 요점이다.
            run.steps = [*steps, {
                "id": node_id, "type": kind, "status": "waiting",
                "summary": str(node.get("title") or node_id)[:_SUMMARY], "ms": 0,
            }]
            run.outputs = outputs
            run.status = WorkflowRunStatus.waiting
            run.pending_node = node_id
            db.commit()
            return

        step_started = time.monotonic()
        try:
            result = _run_node(db, workflow, node, _input_of(node_id, edges, outputs))
        except Exception as e:  # noqa: BLE001 — 단계 실패는 실행 실패다(이유를 싣는다)
            steps.append({"id": node_id, "type": kind, "status": "failed",
                          "summary": f"{type(e).__name__}: {e}"[:_SUMMARY],
                          "ms": int((time.monotonic() - step_started) * 1000)})
            run.steps = steps
            run.outputs = outputs
            run.status = WorkflowRunStatus.failed
            run.error = f"{node_id}: {e}"[:2000]
            run.finished_at = utcnow()
            db.commit()
            return
        outputs[node_id] = result
        done.add(node_id)
        steps.append({
            "id": node_id, "type": kind, "status": "ok",
            "summary": (result.get("text") or "")[:_SUMMARY]
            or f"파일 {len(result.get('paths') or [])}건",
            "ms": int((time.monotonic() - step_started) * 1000),
        })
        run.steps = steps
        run.outputs = outputs
        db.commit()  # 단계마다 남긴다 — 화면이 진행을 보여 줄 근거다

    run.status = WorkflowRunStatus.succeeded
    run.finished_at = utcnow()
    db.commit()


def _edge_live(edge: dict, outputs: dict, skipped: set[str]) -> bool:
    """이 연결로 흐름이 왔는가 — 분기의 선택과 건너뛴 단계를 함께 본다."""
    src = str(edge["from"])
    if src in skipped or src not in outputs:
        return False
    case = edge.get("case")
    if case is None:
        return True
    return (outputs[src].get("case") or "") == case


def _input_of(node_id: str, edges: list, outputs: dict) -> dict:
    """들어오는 엣지의 출처 출력을 순서대로 이어 붙인다."""
    texts: list[str] = []
    paths: list[str] = []
    for edge in edges:
        if str(edge["to"]) != node_id:
            continue
        source = outputs.get(str(edge["from"]))
        if not source:
            continue
        if source.get("text"):
            texts.append(str(source["text"]))
        paths.extend(source.get("paths") or [])
    return {"text": "\n\n".join(texts)[:MAX_TEXT], "paths": paths[:MAX_PATHS]}


def _run_node(db: Session, workflow: Workflow, node: dict, data: dict) -> dict:
    kind = str(node.get("type"))
    if kind == "storage.list":
        return _node_storage_list(node, workflow.organization_id)
    if kind == "docs.search":
        return _node_docs_search(node, workflow.organization_id)
    if kind == "graph.find":
        return _node_graph_find(node, workflow.organization_id)
    if kind == "doc.read":
        return _node_doc_read(node, data, workflow.organization_id)
    if kind == "llm":
        return _node_llm(db, workflow, node, data)
    if kind == "mcp.tool":
        return _node_mcp(db, workflow, node, data)
    if kind == "branch":
        return _node_branch(db, workflow, node, data)
    if kind == "storage.write":
        return _node_storage_write(node, data, workflow.organization_id)
    raise WorkflowError(f"실행할 수 없는 종류입니다: {kind}")


def _store_or_fail(name: str, organization_id: int) -> storage.Store:
    target = storage.store(str(name))
    if target is None or target.organization_id != organization_id:
        raise WorkflowError(f"저장소 '{name}'가 없습니다.")
    return target


def _node_storage_list(node: dict, organization_id: int) -> dict:
    target = _store_or_fail(node["store"], organization_id)
    prefix = str(node.get("prefix") or "")
    suffix = str(node.get("suffix") or "").lower()
    limit = _as_int(node.get("limit"), MAX_PATHS)
    paths = [
        f["path"] for f in storage.list_files(target.root)
        if f["path"].startswith(prefix) and f["path"].lower().endswith(suffix)
    ][:limit]
    return {"text": "\n".join(paths), "paths": paths}


def _node_docs_search(node: dict, organization_id: int) -> dict:
    target = _store_or_fail(node["store"], organization_id)
    found = docsearch.search(target.name, str(node["query"]),
                             limit=_as_int(node.get("limit"), 10))
    lines: list[str] = []
    paths: list[str] = []
    for hit in found.get("hits", []):
        paths.append(hit["path"])
        lines.append(f"## {hit['path']}\n" + "\n".join(hit.get("snippets") or []))
    return {"text": "\n\n".join(lines)[:MAX_TEXT], "paths": paths}


def _node_graph_find(node: dict, organization_id: int) -> dict:
    """온톨로지에서 노드를 찾고 **그 노드가 있는 문서 경로**를 함께 낸다.

    색인이 이미 뽑아 둔 구조(절·용어·표 머리글)를 워크플로가 쓸 수 있게 하는 자리다. 전까지는
    그래프를 아무 단계도 보지 않았다 — 문서 28,301개에서 뽑은 노드가 워크플로에는 없는 것과
    같았다. 경로를 함께 내는 것이 요점이다: 출력 규약이 하나라서(text·paths) 다음 단계에
    doc.read를 두면 "이 표 양식이 있는 문서를 전부 읽어" 가 바로 된다.

    문서별 행을 쓰는 이유(node_search가 아니라 find_nodes): 여기서 필요한 것은 이름이 아니라
    **어느 문서에 있나**다.
    """
    target = _store_or_fail(node["store"], organization_id)
    rows = docsearch.find_nodes(target.name, str(node.get("kind") or ""),
                                str(node.get("q") or ""),
                                _as_int(node.get("limit"), 30))
    lines = [f"{r['kind']} · {r['name']}" + (f" ({r['detail']})" if r.get("detail") else "")
             + f" — {r['path']}" for r in rows]
    paths: list[str] = []
    for row in rows:
        if row["path"] not in paths:
            paths.append(row["path"])
    return {"text": "\n".join(lines)[:MAX_TEXT], "paths": paths[:MAX_PATHS]}


def _node_doc_read(node: dict, data: dict, organization_id: int) -> dict:
    # path를 적었으면 그 파일, 안 적었으면 앞 단계가 준 경로들.
    store_name = str(node.get("store") or "")
    targets = [str(node["path"])] if node.get("path") else list(data.get("paths") or [])
    if not targets:
        raise WorkflowError("읽을 경로가 없습니다 — path를 적거나 앞 단계에서 경로를 받으세요.")
    limit = _as_int(node.get("limit"), 20)
    blocks: list[str] = []
    used: list[str] = []
    for rel in targets[:limit]:
        target = _store_or_fail(store_name, organization_id) if store_name else _owner_of(rel, organization_id)
        source = storage.resolve(target.root, rel)
        if not source.is_file():
            blocks.append(f"## {rel}\n(파일이 없습니다)")
            continue
        blocks.append(f"## {rel}\n{docready.read(target.name, rel, source)}")
        used.append(rel)
    return {"text": "\n\n".join(blocks)[:MAX_TEXT], "paths": used}


def _owner_of(rel: str, organization_id: int) -> storage.Store:
    """어느 저장소의 경로인지 — 앞 단계가 경로만 줬을 때."""
    for candidate in storage.visible_stores():
        if candidate.organization_id == organization_id and (candidate.root / Path(rel)).is_file():
            return candidate
    raise WorkflowError(f"'{rel}'가 어느 저장소에도 없습니다 — store를 적으세요.")


def _provider_or_fail(db: Session, workflow: Workflow, name: str) -> LlmProvider:
    if name:
        provider = db.execute(
            select(LlmProvider).where(LlmProvider.name == str(name))
        ).scalar_one_or_none()
        if provider is None:
            raise WorkflowError(f"LLM 프로바이더 '{name}'가 없습니다.")
    else:
        provider = llm.default_provider(db)
        if provider is None:
            raise WorkflowError(
                "기본 LLM 프로바이더가 없습니다 — LLM 관리에서 「설정」을 누르세요.")
    # 조직 범위는 검증과 같은 규칙으로 본다(전역 또는 이 조직).
    if provider.organization_id not in (None, workflow.organization_id):
        raise WorkflowError(f"'{provider.name}'는 이 조직에서 쓸 수 없습니다.")
    return provider


def _node_llm(db: Session, workflow: Workflow, node: dict, data: dict) -> dict:
    provider = _provider_or_fail(db, workflow, str(node.get("provider") or ""))
    context = data.get("text") or ""
    messages = [
        {"role": "system", "content":
            "워크플로의 한 단계를 수행한다. 주어진 자료만 근거로 쓰고, 자료에 없으면 "
            "없다고 적는다. 지어내지 않는다."},
        {"role": "user", "content":
            f"{node['prompt']}\n\n--- 앞 단계 자료 ---\n{context or '(없음)'}"},
    ]
    text = llm.chat_completion(provider, messages, db)
    return {"text": text[:MAX_TEXT], "paths": list(data.get("paths") or [])}


def _node_mcp(db: Session, workflow: Workflow, node: dict, data: dict) -> dict:
    module = db.execute(
        select(Module).where(Module.name == str(node["module"]))
    ).scalar_one_or_none()
    if module is None:
        raise WorkflowError(f"모듈 '{node['module']}'가 없습니다.")
    if module.organization_id not in (None, workflow.organization_id):
        raise WorkflowError(f"모듈 '{module.name}'를 이 조직에서 쓸 수 없습니다.")
    config = modules_service.decrypt_config(module.config or {})
    url = str(config.get("url") or "")
    if not url:
        raise WorkflowError(f"모듈 '{module.name}'에 url이 없습니다.")
    args = dict(node.get("args") or {})
    # 앞 단계 텍스트를 쓰고 싶다는 표시 — 인자에 "$입력"이라고 적는다.
    for key, value in args.items():
        if value == "$입력":
            args[key] = data.get("text") or ""
    text = mcp_client.call_tool(url, config.get("api_key"), str(node["tool"]), args)
    return {"text": str(text)[:MAX_TEXT], "paths": list(data.get("paths") or [])}


def _node_branch(db: Session, workflow: Workflow, node: dict, data: dict) -> dict:
    when = dict(node.get("when") or {})
    kind = str(when.get("kind") or "")
    text = data.get("text") or ""
    if kind == "contains":
        answer = str(when["text"]) in text
    elif kind == "empty":
        answer = not text.strip()
    elif kind == "llm":
        provider = _provider_or_fail(db, workflow, str(when.get("provider") or ""))
        reply = llm.chat_completion(provider, [
            {"role": "system", "content":
                "질문에 '예' 또는 '아니오' 한 낱말로만 답한다. 자료에 근거가 없으면 '아니오'."},
            {"role": "user", "content":
                f"{when['question']}\n\n--- 자료 ---\n{text or '(없음)'}"},
        ], db)
        answer = reply.strip().startswith(("예", "Yes", "yes", "참", "true", "True"))
    else:
        raise WorkflowError(f"모르는 분기 조건입니다: {kind}")
    case = CASES[0] if answer else CASES[1]
    return {"text": text, "paths": list(data.get("paths") or []), "case": case}


def _node_storage_write(node: dict, data: dict, organization_id: int) -> dict:
    target = _store_or_fail(node["store"], organization_id)
    if target.read_only:
        raise WorkflowError(f"'{target.name}'는 읽기 전용 저장소입니다.")
    body = (data.get("text") or "").encode("utf-8")
    saved = storage.write_file(target, str(node["path"]), body)
    return {"text": f"{target.name}:{saved} ({len(body)} bytes)", "paths": [saved]}


def _as_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, MAX_PATHS))


def spec_summary(spec: dict) -> dict:
    """화면 목록에 쓸 요약 — 단계 수와 사람 단계 유무."""
    nodes = [n for n in (spec or {}).get("nodes", []) if isinstance(n, dict)]
    return {
        "node_count": len(nodes),
        "human_steps": sum(1 for n in nodes if n.get("type") == "human"),
        "types": sorted({str(n.get("type")) for n in nodes if n.get("type")}),
    }


def spec_as_text(spec: dict) -> str:
    """LLM에 현재 스펙을 보일 때 쓰는 모양 — 보기 좋은 JSON 하나."""
    return json.dumps(spec or {"nodes": [], "edges": []}, ensure_ascii=False, indent=2)
