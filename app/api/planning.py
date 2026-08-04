"""에이전트 기획(Agent Planning) — 단계별 대화(문서 초안)·확정(Gitea 커밋)·제약·모니터링·MCP 서버.

에이전트 빌더(app/api/llm.py)의 채팅 파이프라인을 재사용하되, 출력은 코드 diff가 아니라
단계 산출물 문서다. 확정 시 산출물을 프로젝트 Gitea 리포에 커밋하고(services/workspace),
DB(PlanArtifact)에는 위치·커밋·확정 포인터만 남긴다. 빌드는 외부 개발도구에서 수행하며,
플랫폼은 가용 모듈 제약(guardrail)과 MCP 서버·모니터링만 제공한다.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import (
    ApiKey,
    AuditEvent,
    ChatMessage,
    ChatSession,
    LlmProvider,
    PlanArtifact,
    PlanStage,
    Project,
)
from ..schemas import (
    PlanArtifactOut,
    PlanConfirmIn,
    PlanMessageIn,
    PlanMessageReply,
    PlanSessionCreate,
    PlanSessionOut,
)
from ..security import require_api_key
from ..services import a2a as a2a_service
from ..services import codemap as codemap_service
from ..services import llm as llm_service
from ..services import modules as modules_service
from ..services import planning as planning_service
from ..services import workspace

router = APIRouter(tags=["planning"])


def _parse_stage(stage: str) -> PlanStage:
    try:
        return PlanStage(stage)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"unknown plan stage: {stage}")


def _artifacts_out(db: Session, session_id: int) -> list[PlanArtifactOut]:
    """모든 단계를 순서대로 반환하되, 아직 없는 단계는 미확정 기본값으로 채운다(진행단계 표시용)."""
    rows = db.execute(
        select(PlanArtifact).where(PlanArtifact.session_id == session_id)
    ).scalars().all()
    by_stage = {a.stage: a for a in rows}
    out: list[PlanArtifactOut] = []
    for stage in planning_service.STAGE_ORDER:
        a = by_stage.get(stage)
        out.append(PlanArtifactOut(
            stage=stage.value,
            title=planning_service.stage_title(stage),
            repo_path=a.repo_path if a else planning_service.stage_repo_path(stage),
            commit_sha=a.commit_sha if a else None,
            confirmed=bool(a and a.confirmed),
        ))
    return out


def _session_out(db: Session, session: ChatSession) -> PlanSessionOut:
    project = db.get(Project, session.project_id)
    provider = db.get(LlmProvider, session.provider_id)
    return PlanSessionOut(
        id=session.id,
        branch=session.branch,
        provider=provider.name if provider else "",
        project_id=session.project_id,
        project_name=project.name if project else "",
        artifacts=_artifacts_out(db, session.id),
    )


@router.post("/plan/sessions", response_model=PlanSessionOut, status_code=201)
def create_plan_session(
    body: PlanSessionCreate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, body.project_id)
    provider = db.get(LlmProvider, body.provider_id)
    if project is None or provider is None:
        raise HTTPException(status_code=404, detail="project or provider not found")
    llm_service.require_provider_access(provider, project, key)
    session = ChatSession(project_id=project.id, provider_id=provider.id, branch="")
    db.add(session)
    db.commit()
    session.branch = body.branch or f"paas/plan-{session.id}"
    db.commit()
    audit.record(db, key.name, "plan.session.create", project.name,
                 {"provider": provider.name, "branch": session.branch})
    return _session_out(db, session)


@router.get("/plan/sessions/{session_id}", response_model=PlanSessionOut)
def get_plan_session(
    session_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _session_out(db, session)


@router.post("/plan/sessions/{session_id}/stages/{stage}/messages", response_model=PlanMessageReply)
async def post_plan_message(
    session_id: int,
    stage: str,
    body: PlanMessageIn,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    plan_stage = _parse_stage(stage)
    project = db.get(Project, session.project_id)
    provider = db.get(LlmProvider, session.provider_id)
    llm_service.require_provider_access(provider, project, key)

    # 앞 단계 확정을 전제로 한다(순차 진행 강제)
    prev = planning_service.prev_stage(plan_stage)
    if prev is not None and not _is_confirmed(db, session_id, prev):
        raise HTTPException(
            status_code=409,
            detail=f"앞 단계('{planning_service.stage_title(prev)}')를 먼저 확정하세요.",
        )

    system_prompt = planning_service.PLANNING_SYSTEM_PROMPT + "\n\n" + planning_service.stage_prompt(plan_stage)
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    context_parts = [f"Project: {project.name} (type={project.type.value})"]
    workdir = workspace.workdir_for(project)
    if workdir.exists():
        context_parts.append(
            "=== PROJECT STACK & DEPENDENCIES ===\n"
            + json.dumps(workspace.detect_project_stack_and_deps(workdir), indent=2, ensure_ascii=False)
        )

    # 가용 모듈 제약(guardrail) — 모든 단계에 주입, 솔루션 구성 단계의 핵심 제약
    constraints = planning_service.build_constraints(db, project)
    context_parts.append("=== 가용 모듈 제약 (사용 가능 자원·게이트웨이 규칙) ===\n"
                         + planning_service.render_constraints_doc(constraints))

    # 앞 단계에서 확정된 산출물 본문 누적 주입(리포 워킹카피에서 읽음)
    if workdir.exists():
        confirmed_paths = [
            planning_service.stage_repo_path(s)
            for s in planning_service.STAGE_ORDER
            if s != plan_stage and _is_confirmed(db, session_id, s)
        ]
        for path, content in workspace.read_context_files(workdir, confirmed_paths).items():
            context_parts.append(f"=== 확정된 앞 단계 산출물: {path} ===\n{content}")
        # 코드 구조 개요
        try:
            outline = codemap_service.render_outline(codemap_service.build_code_map(workdir))
            context_parts.append("=== CODE STRUCTURE (OUTLINE) ===\n" + outline)
        except Exception:  # noqa: BLE001
            pass
        for path, content in workspace.read_context_files(workdir, body.files).items():
            context_parts.append(f"--- {path} ---\n{content}")

    messages.append({"role": "system", "content": "\n\n".join(context_parts)})

    history = db.execute(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
    ).scalars()
    messages.extend({"role": m.role, "content": m.content} for m in history)
    messages.append({"role": "user", "content": body.content})

    try:
        reply = await asyncio.to_thread(llm_service.chat_completion, provider, messages, db)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"llm call failed: {e}")

    used_modules: list[str] = []
    reply_upper = reply.upper()
    for res_item in constraints.get("available_resources") or []:
        r_name = str(res_item.get("name", ""))
        if r_name and (r_name in reply or r_name.upper().replace("-", "_") in reply_upper):
            used_modules.append(r_name)

    db.add(ChatMessage(session_id=session_id, role="user", content=body.content))
    db.add(ChatMessage(session_id=session_id, role="assistant", content=reply))
    db.commit()
    return PlanMessageReply(reply=reply, used_modules=used_modules)


@router.post("/plan/sessions/{session_id}/stages/{stage}/confirm", response_model=PlanArtifactOut)
async def confirm_plan_stage(
    session_id: int,
    stage: str,
    body: PlanConfirmIn,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    plan_stage = _parse_stage(stage)
    project = db.get(Project, session.project_id)
    if not body.content.strip():
        raise HTTPException(status_code=422, detail="확정할 산출물 본문이 비어 있습니다.")

    prev = planning_service.prev_stage(plan_stage)
    if prev is not None and not _is_confirmed(db, session_id, prev):
        raise HTTPException(
            status_code=409,
            detail=f"앞 단계('{planning_service.stage_title(prev)}')를 먼저 확정하세요.",
        )

    repo_path = planning_service.stage_repo_path(plan_stage)
    message = f"plan({plan_stage.value}): {planning_service.stage_title(plan_stage)} 확정 (session #{session_id})"
    try:
        sha = await asyncio.to_thread(
            workspace.write_and_commit, project, session.branch, repo_path, body.content, message,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Gitea 커밋 실패: {e}")

    artifact = db.execute(
        select(PlanArtifact).where(
            PlanArtifact.session_id == session_id, PlanArtifact.stage == plan_stage
        )
    ).scalar_one_or_none()
    if artifact is None:
        artifact = PlanArtifact(session_id=session_id, stage=plan_stage, repo_path=repo_path)
        db.add(artifact)
    artifact.repo_path = repo_path
    artifact.commit_sha = sha
    artifact.confirmed = True
    db.commit()
    audit.record(db, key.name, "plan.stage.confirm", project.name,
                 {"stage": plan_stage.value, "sha": sha, "branch": session.branch})
    return PlanArtifactOut(
        stage=plan_stage.value, title=planning_service.stage_title(plan_stage),
        repo_path=repo_path, commit_sha=sha, confirmed=True,
    )


@router.get("/plan/sessions/{session_id}/repo")
def get_plan_repo(
    session_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """개발도구(VSCode·Claude·Antigravity) 연동용 리포 정보. git_url은 admin에만 노출."""
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    project = db.get(Project, session.project_id)
    return {
        "clone_url": project.git_url if key.is_admin else None,
        "branch": session.branch,
        "artifact_dir": planning_service.ARTIFACT_DIR,
        "artifacts": [a.model_dump() for a in _artifacts_out(db, session_id)],
    }


@router.get("/plan/projects/{project_id}/constraints")
def get_plan_constraints(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """외부 빌드 guardrail이 되는 가용 모듈 제약(데이터 + 마크다운 문서)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    constraints = planning_service.build_constraints(db, project)
    return {**constraints, "document": planning_service.render_constraints_doc(constraints)}


@router.get("/plan/sessions/{session_id}/build-status")
def get_plan_build_status(
    session_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """외부 빌드 모니터링 — 프로젝트 관련 감사 이벤트(커밋 트리거·모듈 사용·기획 확정)를 집계."""
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    project = db.get(Project, session.project_id)
    events = db.execute(
        select(AuditEvent)
        .where(AuditEvent.target == project.name)
        .order_by(AuditEvent.id.desc())
        .limit(50)
    ).scalars().all()
    return {
        "project": project.name,
        "branch": session.branch,
        "events": [
            {"actor": e.actor, "action": e.action, "detail": e.detail,
             "created_at": e.created_at.isoformat() if e.created_at else None}
            for e in events
        ],
    }


# --- MCP 서버 (외부 빌드 도구 연동) ---
# 플랫폼은 기존에 MCP 클라이언트였으나(services/mcp_client.py), 외부 빌드 도구(MCP
# 클라이언트)가 접속해 제약·모듈을 조회하고 진행을 보고할 수 있도록 여기서 MCP 서버를
# 노출한다. JSON-RPC 2.0 / tools/list · tools/call 최소 구현이며, 노출 데이터는 기존
# A2A 카드·모듈 메타를 매핑한다(신규 데이터 소스 없음).

_MCP_TOOLS = [
    {
        "name": "get_constraints",
        "description": "이 프로젝트의 가용 모듈 제약(외부 빌드 guardrail) 문서를 반환한다.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_available_modules",
        "description": "이 프로젝트에서 사용 가능한 내부 모듈/A2A 능력 목록을 반환한다.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "report_build_progress",
        "description": "외부 빌드 진행 상황을 플랫폼에 보고한다(모니터링에 집계됨).",
        "inputSchema": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    },
]


def _mcp_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


@router.post("/plan/projects/{project_id}/mcp")
async def plan_mcp_server(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """외부 빌드 도구가 접속하는 MCP 서버 엔드포인트(JSON-RPC 2.0)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}

    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") or {}

    def _ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    if method == "initialize":
        return _ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"paas-plan-{project.name}", "version": "0.1.0"},
        })
    if method == "tools/list":
        return _ok({"tools": _MCP_TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "get_constraints":
            constraints = planning_service.build_constraints(db, project)
            return _ok(_mcp_text(planning_service.render_constraints_doc(constraints)))
        if name == "list_available_modules":
            constraints = planning_service.build_constraints(db, project)
            return _ok(_mcp_text(json.dumps(constraints["bound_agents"], ensure_ascii=False)))
        if name == "report_build_progress":
            note = str(args.get("note", ""))[:1000]
            audit.record(db, key.name, "plan.build.progress", project.name, {"note": note})
            return _ok(_mcp_text("recorded"))
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"unknown tool: {name}"}}

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"unknown method: {method}"}}


def _is_confirmed(db: Session, session_id: int, stage: PlanStage) -> bool:
    a = db.execute(
        select(PlanArtifact).where(
            PlanArtifact.session_id == session_id, PlanArtifact.stage == stage
        )
    ).scalar_one_or_none()
    return bool(a and a.confirmed)
