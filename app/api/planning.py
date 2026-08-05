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
    BuildTask,
    BuildTaskStatus,
    ChatMessage,
    ChatSession,
    LlmProvider,
    PlanArtifact,
    PlanStage,
    Project,
)
from ..schemas import (
    BuildTaskOut,
    BuildTaskUpdate,
    ComplianceOut,
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
from ..services import compliance as compliance_service
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
            default_request=planning_service.stage_request(stage),
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

    # 역할 → 기획·구현 원칙(플랫폼 표준 문서) → 이번 단계 지시 순으로 쌓는다.
    system_prompt = "\n\n".join(filter(None, [
        planning_service.PLANNING_SYSTEM_PROMPT,
        llm_service.agent_principles_prompt(),
        planning_service.stage_prompt(plan_stage),
    ]))
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

    # 앞 단계에서 확정된 산출물 본문을 순서대로 주입 — 각 단계는 이전 단계 문서를 근거로 쓴다.
    # 세션 브랜치에 커밋된 본문을 읽으므로 워킹카피가 어떤 브랜치에 있든 동일하게 참조된다.
    for idx, s in enumerate(planning_service.prev_stages(plan_stage), start=1):
        if not _is_confirmed(db, session_id, s):
            continue
        path = planning_service.stage_repo_path(s)
        content = None
        if workdir.exists():
            content = workspace.read_file_at_ref(workdir, session.branch, path)
            if content is None:
                content = workspace.read_context_files(workdir, [path]).get(path)
        if content:
            context_parts.append(
                f"=== 이전 단계 확정 산출물 {idx}. {planning_service.stage_title(s)} ({path}) ===\n{content}"
            )

    # git 파일 목록은 기본 참조, 내용 확인이 필요한 파일만 골라 본문을 덧붙인다.
    context_files: list[str] = []
    if workdir.exists():
        tree = workspace.file_tree(workdir, limit=planning_service.MAX_TREE_FILES)
        if tree:
            context_parts.append("=== GIT 파일 목록 (리포 추적 파일) ===\n" + "\n".join(tree))
        # 코드 구조 개요
        try:
            outline = codemap_service.render_outline(codemap_service.build_code_map(workdir))
            context_parts.append("=== CODE STRUCTURE (OUTLINE) ===\n" + outline)
        except Exception:  # noqa: BLE001
            pass
        selected = await asyncio.to_thread(
            planning_service.select_context_files, provider, db, tree, body.content, body.files,
        )
        for path, content in workspace.read_context_files(workdir, selected).items():
            context_parts.append(f"--- {path} ---\n{content}")
            context_files.append(path)

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
    return PlanMessageReply(reply=reply, used_modules=used_modules, context_files=context_files)


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

    # 커밋 후 git 상태에 따라 PR 생성·머지를 자동 수행(작업 브랜치일 때만).
    git_result = await asyncio.to_thread(
        planning_service.auto_pull_request, project, session.branch, message,
        f"에이전트 기획 산출물 확정: {repo_path}",
    )

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
                 {"stage": plan_stage.value, "sha": sha, "branch": session.branch,
                  "git_action": git_result["action"]})
    return PlanArtifactOut(
        stage=plan_stage.value, title=planning_service.stage_title(plan_stage),
        repo_path=repo_path, commit_sha=sha, confirmed=True,
        default_request=planning_service.stage_request(plan_stage),
        git_action=git_result["action"],
        git_detail=git_result.get("detail"),
        pull_request_url=git_result.get("url"),
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


# --- 작업 지시(work order) — 확정 산출물을 외주 빌더가 집어갈 단위로 쪼개 추적한다 ---


def _task_out(t: BuildTask) -> BuildTaskOut:
    return BuildTaskOut(
        id=t.id, title=t.title, detail=t.detail, verify=t.verify,
        status=t.status.value, note=t.note, commit_sha=t.commit_sha,
    )


def _session_tasks(db: Session, session_id: int) -> list[BuildTask]:
    return db.execute(
        select(BuildTask).where(BuildTask.session_id == session_id).order_by(BuildTask.id)
    ).scalars().all()


@router.post("/plan/sessions/{session_id}/tasks/generate", response_model=list[BuildTaskOut])
async def generate_build_tasks(
    session_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """확정된 기획 산출물에서 외주 빌드 작업 지시를 만든다.

    아직 아무도 착수하지 않았을 때만 다시 만든다 — 진행 중인 지시를 덮어쓰면 외부
    빌더가 보고 있던 작업이 말없이 사라진다.
    """
    session = db.get(ChatSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    project = db.get(Project, session.project_id)
    provider = db.get(LlmProvider, session.provider_id)
    llm_service.require_provider_access(provider, project, key)

    existing = _session_tasks(db, session_id)
    if any(t.status != BuildTaskStatus.pending for t in existing):
        raise HTTPException(status_code=409, detail="이미 진행 중인 작업 지시가 있습니다.")

    confirmed = [s for s in planning_service.STAGE_ORDER if _is_confirmed(db, session_id, s)]
    if not confirmed:
        raise HTTPException(status_code=409, detail="확정된 산출물이 없습니다. 단계를 먼저 확정하세요.")

    workdir = workspace.workdir_for(project)
    documents = []
    for stage in confirmed:
        path = planning_service.stage_repo_path(stage)
        content = workspace.read_file_at_ref(workdir, session.branch, path) if workdir.exists() else None
        if content:
            documents.append(f"=== {planning_service.stage_title(stage)} ({path}) ===\n{content}")
    if not documents:
        raise HTTPException(status_code=409, detail="확정 산출물 본문을 리포에서 읽지 못했습니다.")

    constraints = planning_service.build_constraints(db, project)
    items = await asyncio.to_thread(
        planning_service.decompose_tasks, provider, db, "\n\n".join(documents),
        planning_service.render_constraints_doc(constraints),
    )
    if not items:
        raise HTTPException(status_code=502, detail="작업 지시 생성에 실패했습니다.")

    for t in existing:
        db.delete(t)
    for item in items:
        db.add(BuildTask(
            project_id=project.id, session_id=session_id,
            title=item["title"], detail=item["detail"], verify=item["verify"],
        ))
    db.commit()
    audit.record(db, key.name, "plan.tasks.generate", project.name, {"count": len(items)})
    return [_task_out(t) for t in _session_tasks(db, session_id)]


@router.get("/plan/sessions/{session_id}/tasks", response_model=list[BuildTaskOut])
def list_build_tasks(
    session_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    if db.get(ChatSession, session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    return [_task_out(t) for t in _session_tasks(db, session_id)]


@router.patch("/plan/tasks/{task_id}", response_model=BuildTaskOut)
def update_build_task(
    task_id: int,
    body: BuildTaskUpdate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    task = db.get(BuildTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _apply_task_update(db, key.name, task, body.status, body.note, body.commit_sha)
    return _task_out(task)


def _apply_task_update(
    db: Session, actor: str, task: BuildTask,
    status: str | None, note: str | None, commit_sha: str | None,
) -> None:
    """작업 지시 갱신 — 콘솔(PATCH)과 외부 빌더(MCP)가 같은 경로를 쓴다."""
    if status is not None:
        try:
            task.status = BuildTaskStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"unknown status: {status}")
    if note is not None:
        task.note = note[:2000]
    if commit_sha is not None:
        task.commit_sha = commit_sha[:40]
    db.commit()
    project = db.get(Project, task.project_id)
    audit.record(db, actor, "plan.task.update", project.name if project else str(task.project_id),
                 {"task_id": task.id, "status": task.status.value})


@router.get("/plan/projects/{project_id}/compliance", response_model=ComplianceOut)
def get_plan_compliance(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """외주 빌드 결과 검증 — LLM·모듈 사용이 제약을 지켰는지 보고, 위반 시 수정 지시 프롬프트를 만든다."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return _compliance_out(db, project)


def _compliance_out(db: Session, project: Project) -> ComplianceOut:
    constraints = planning_service.build_constraints(db, project)
    findings = compliance_service.scan(workspace.workdir_for(project), constraints)
    return ComplianceOut(
        project=project.name,
        findings=findings,
        summary=compliance_service.summarize(findings),
        builder_prompt=compliance_service.builder_prompt(
            findings, planning_service.render_constraints_doc(constraints)
        ),
    )


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
    {
        "name": "read_artifact",
        "description": "확정된 기획 산출물 본문을 읽는다(clone 없이). stage: spec|architecture|solution|principles",
        "inputSchema": {
            "type": "object",
            "properties": {"stage": {"type": "string"}},
            "required": ["stage"],
        },
    },
    {
        "name": "list_tasks",
        "description": "이 프로젝트의 빌드 작업 지시(work order) 목록과 상태를 반환한다.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_task",
        "description": "작업 지시 상태를 갱신한다. status: pending|in_progress|done|blocked",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "submit_build_result",
        "description": "구현 결과(커밋 sha·요약)를 제출한다. task_id를 주면 그 작업을 완료 처리한다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "commit_sha": {"type": "string"},
                "summary": {"type": "string"},
                "task_id": {"type": "integer"},
            },
            "required": ["commit_sha"],
        },
    },
    {
        "name": "request_clarification",
        "description": (
            "기획이 모호해 진행할 수 없을 때 질의한다. 질의는 기획 세션 대화에 남아 "
            "다음 초안에 반영되고, task_id를 주면 그 작업은 blocked가 된다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "task_id": {"type": "integer"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "check_compliance",
        "description": (
            "커밋된 코드의 LLM·모듈 사용이 제약을 지켰는지 검사한다. "
            "위반이 있으면 그대로 따를 수정 지시가 함께 온다."
        ),
        "inputSchema": {"type": "object", "properties": {}},
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
        if name == "read_artifact":
            try:
                stage = PlanStage(str(args.get("stage", "")))
            except ValueError:
                return _mcp_error(req_id, f"unknown stage: {args.get('stage')}")
            content = _read_confirmed_artifact(db, project, stage)
            if content is None:
                return _mcp_error(req_id, f"확정된 산출물이 없습니다: {args.get('stage')}")
            return _ok(_mcp_text(content))
        if name == "list_tasks":
            tasks = db.execute(
                select(BuildTask).where(BuildTask.project_id == project.id).order_by(BuildTask.id)
            ).scalars().all()
            text = json.dumps(
                [_task_out(t).model_dump() for t in tasks], ensure_ascii=False, indent=2)
            # 최근 push에서 잡힌 위반은 작업 목록을 볼 때 함께 알린다 — 빌더가 폴링하지 않아도 안다.
            warning = _latest_compliance_warning(db, project)
            return _ok(_mcp_text(f"{warning}\n\n{text}" if warning else text))
        if name in ("update_task", "submit_build_result", "request_clarification"):
            return _mcp_build_report(db, key.name, project, name, args, _ok, req_id)
        if name == "check_compliance":
            result = _compliance_out(db, project)
            audit.record(db, key.name, "plan.build.compliance", project.name, {
                "status": "warning" if result.findings else "clean",
                "summary": result.summary,
            })
            if not result.findings:
                return _ok(_mcp_text("위반 없음 — 제약을 지켰습니다."))
            return _ok(_mcp_text(result.builder_prompt))
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"unknown tool: {name}"}}

    return {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"unknown method: {method}"}}


def _mcp_error(req_id, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": message}}


def _latest_compliance_warning(db: Session, project: Project) -> str:
    """가장 최근 컴플라이언스 검사에서 위반이 남아 있으면 그 요약 한 줄.

    검사 결과는 감사 이벤트가 원천이라 별도 저장소를 두지 않는다 — 다시 검사해 깨끗하면
    최신 이벤트가 clean이 되어 경고도 자연히 사라진다.
    """
    event = db.execute(
        select(AuditEvent)
        .where(AuditEvent.target == project.name,
               AuditEvent.action == "plan.build.compliance")
        .order_by(AuditEvent.id.desc())
    ).scalars().first()
    summary = (event.detail or {}).get("summary") if event else None
    if not summary:
        return ""
    return (
        "⚠️ 최근 커밋에서 제약 위반이 감지됐습니다(머지·배포는 막지 않음): "
        f"{json.dumps(summary, ensure_ascii=False)}\n"
        "check_compliance를 호출해 수정 지시를 받으세요."
    )


def _latest_session(db: Session, project_id: int) -> ChatSession | None:
    return db.execute(
        select(ChatSession).where(ChatSession.project_id == project_id)
        .order_by(ChatSession.id.desc())
    ).scalars().first()


def _read_confirmed_artifact(db: Session, project: Project, stage: PlanStage) -> str | None:
    """확정된 단계 산출물 본문 — 커밋된 세션 브랜치에서 읽는다(가장 최근 확정 우선)."""
    row = db.execute(
        select(PlanArtifact, ChatSession)
        .join(ChatSession, PlanArtifact.session_id == ChatSession.id)
        .where(
            ChatSession.project_id == project.id,
            PlanArtifact.stage == stage,
            PlanArtifact.confirmed.is_(True),
        )
        .order_by(PlanArtifact.id.desc())
    ).first()
    if row is None:
        return None
    artifact, session = row
    workdir = workspace.workdir_for(project)
    if not workdir.exists():
        return None
    for ref in (session.branch, project.branch):
        content = workspace.read_file_at_ref(workdir, ref, artifact.repo_path)
        if content:
            return content
    return workspace.read_context_files(workdir, [artifact.repo_path]).get(artifact.repo_path)


def _mcp_build_report(db: Session, actor: str, project: Project, tool: str, args: dict,
                      ok, req_id) -> dict:
    """외부 빌더의 쓰기 3종(작업 갱신·결과 제출·질의)을 한 자리에서 처리한다."""
    task = None
    task_id = args.get("task_id")
    if task_id is not None:
        task = db.get(BuildTask, int(task_id))
        if task is None or task.project_id != project.id:
            return _mcp_error(req_id, f"task not found: {task_id}")

    if tool == "update_task":
        if task is None:
            return _mcp_error(req_id, "task_id가 필요합니다.")
        _apply_task_update(db, actor, task, args.get("status"), args.get("note"), None)
        return ok(_mcp_text(f"task {task.id} → {task.status.value}"))

    if tool == "submit_build_result":
        sha = str(args.get("commit_sha", ""))[:40]
        summary = str(args.get("summary", ""))[:1000]
        audit.record(db, actor, "plan.build.result", project.name,
                     {"commit_sha": sha, "summary": summary})
        if task is not None:
            _apply_task_update(db, actor, task, BuildTaskStatus.done.value, summary, sha)
        return ok(_mcp_text("recorded"))

    # request_clarification — 질의를 기획 세션 대화에 남겨 다음 초안에 반영되게 한다.
    question = str(args.get("question", "")).strip()[:2000]
    if not question:
        return _mcp_error(req_id, "question이 비어 있습니다.")
    session = _latest_session(db, project.id)
    if session is not None:
        db.add(ChatMessage(session_id=session.id, role="user",
                           content=f"[외주 빌더 질의] {question}"))
        db.commit()
    audit.record(db, actor, "plan.build.clarification", project.name, {"question": question})
    if task is not None:
        _apply_task_update(db, actor, task, BuildTaskStatus.blocked.value, question, None)
    return ok(_mcp_text("질의를 기획 세션에 전달했습니다."))


def _is_confirmed(db: Session, session_id: int, stage: PlanStage) -> bool:
    a = db.execute(
        select(PlanArtifact).where(
            PlanArtifact.session_id == session_id, PlanArtifact.stage == stage
        )
    ).scalar_one_or_none()
    return bool(a and a.confirmed)
