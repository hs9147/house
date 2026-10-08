"""워크플로 창구 — 조직 단위 목록·구성 대화·저장·실행.

**저장은 검증을 통과한 것만 받는다.** 워크플로는 실행되면 저장소에 쓰고 모듈 도구를 부르고
LLM을 호출한다 — 기동 스크립트와 같은 자리에 있으므로, 통과하지 못한 것을 경고만 하고
저장하지 않는다. LLM이 만든 제안도 그대로 저장되지 않는다: 화면이 캔버스에 올리고 사람이
저장을 누른다.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import (
    ApiKey, Organization, Workflow, WorkflowMessage, WorkflowRun, WorkflowRunStatus,
)
from ..security import require_admin, require_api_key
from ..services import bedrock
from ..services import llm as llm_service
from ..services import workflow as workflow_service
from ..services import workflowassess, workflowchat

router = APIRouter(tags=["workflows"])


class WorkflowCreate(BaseModel):
    organization_id: int
    name: str = Field(min_length=1, max_length=64)
    description: str = ""


class WorkflowSave(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    spec: dict
    # 대화에서 읽어 낸 업무(개체·상태·전이·제약) — 스펙과 함께 보관해 다음 검토의 기준이 된다.
    extracted: dict | None = None


class WorkflowRename(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None


class WorkflowChatIn(BaseModel):
    request: str = Field(min_length=1)


class HumanSubmit(BaseModel):
    content: str = ""
    approved: bool = True


def _out(row: Workflow) -> dict:
    return {
        "id": row.id,
        "organization_id": row.organization_id,
        "org_name": row.organization.name if row.organization else "",
        "name": row.name,
        "description": row.description,
        "spec": row.spec or {"nodes": [], "edges": []},
        "extracted": row.extracted or {},
        "version": row.version,
        "summary": workflow_service.spec_summary(row.spec or {}),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _run_out(row: WorkflowRun) -> dict:
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "version": row.version,
        "status": row.status.value,
        "actor": row.actor,
        "steps": row.steps or [],
        "pending_node": row.pending_node,
        "error": row.error,
        "created_at": row.created_at,
        "finished_at": row.finished_at,
    }


def _workflow_or_404(db: Session, workflow_id: int) -> Workflow:
    row = db.get(Workflow, workflow_id)
    if row is None:
        raise HTTPException(status_code=404, detail="워크플로를 찾을 수 없습니다")
    return row


@router.get("/workflows/resources")
def workflow_resources(organization_id: int, db: Session = Depends(get_db),
                       _: ApiKey = Depends(require_api_key)):
    """이 조직이 워크플로에서 쓸 수 있는 것 — 화면의 선택지와 LLM의 사실이 같은 목록이다."""
    if db.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="조직을 찾을 수 없습니다")
    return workflow_service.resources(db, organization_id)


@router.get("/workflows")
def list_workflows(organization_id: int | None = None, db: Session = Depends(get_db),
                   _: ApiKey = Depends(require_api_key)):
    query = select(Workflow).order_by(Workflow.organization_id, Workflow.name)
    if organization_id is not None:
        query = query.where(Workflow.organization_id == organization_id)
    return [_out(row) for row in db.execute(query).scalars()]


@router.post("/workflows", status_code=201)
def create_workflow(body: WorkflowCreate, db: Session = Depends(get_db),
                    admin: ApiKey = Depends(require_admin)):
    if db.get(Organization, body.organization_id) is None:
        raise HTTPException(status_code=404, detail="조직을 찾을 수 없습니다")
    exists = db.execute(
        select(Workflow).where(Workflow.organization_id == body.organization_id,
                               Workflow.name == body.name)
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail="같은 이름의 워크플로가 이미 있습니다")
    # 빈 스펙으로 만든다 — 구성은 대화로 한다. 여기서 템플릿을 심으면 사람이 안 읽고 지나간다.
    row = Workflow(organization_id=body.organization_id, name=body.name,
                   description=body.description, spec={"nodes": [], "edges": []})
    db.add(row)
    db.commit()
    audit.record(db, admin.name, "workflow.create", row.name,
                 {"organization_id": row.organization_id})
    return _out(row)


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: int, db: Session = Depends(get_db),
                 _: ApiKey = Depends(require_api_key)):
    row = _workflow_or_404(db, workflow_id)
    return {
        **_out(row),
        # 지금 저장된 스펙이 여전히 유효한가 — 저장 뒤에 저장소·모듈이 사라질 수 있다.
        "problems": workflow_service.validate(db, row.organization_id, row.spec or {}),
    }


@router.put("/workflows/{workflow_id}")
def save_workflow(workflow_id: int, body: WorkflowSave, db: Session = Depends(get_db),
                  admin: ApiKey = Depends(require_admin)):
    row = _workflow_or_404(db, workflow_id)
    problems = workflow_service.validate(db, row.organization_id, body.spec)
    if problems:
        # 400으로 거부하고 **문제를 전부** 돌려준다 — 화면이 그걸 그대로 보여 준다.
        raise HTTPException(status_code=400, detail={"problems": problems})
    if body.name and body.name != row.name:
        clash = db.execute(
            select(Workflow).where(Workflow.organization_id == row.organization_id,
                                   Workflow.name == body.name, Workflow.id != row.id)
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status_code=409, detail="같은 이름의 워크플로가 이미 있습니다")
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    row.spec = body.spec
    if body.extracted is not None:
        row.extracted = body.extracted
    row.version += 1
    db.commit()
    audit.record(db, admin.name, "workflow.save", row.name,
                 {"version": row.version, **workflow_service.spec_summary(row.spec)})
    return _out(row)


@router.patch("/workflows/{workflow_id}")
def rename_workflow(workflow_id: int, body: WorkflowRename, db: Session = Depends(get_db),
                    admin: ApiKey = Depends(require_admin)):
    """이름·설명만 바꾼다 — 스펙은 건드리지 않는다.

    저장(PUT)으로 개명하지 않는 이유 둘: (1) 저장은 판을 올린다(version++) — 이름을 고친 것은
    새 판이 아니다. (2) 저장은 스펙을 다시 검증한다 — 저장소가 사라져 스펙에 문제가 생긴
    워크플로는 이름조차 못 고치게 된다. 이름을 고치는 일에 그 둘이 끼어들 이유가 없다.
    """
    row = _workflow_or_404(db, workflow_id)
    before = row.name
    if body.name is not None and body.name != row.name:
        clash = db.execute(
            select(Workflow).where(Workflow.organization_id == row.organization_id,
                                   Workflow.name == body.name, Workflow.id != row.id)
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status_code=409, detail="같은 이름의 워크플로가 이미 있습니다")
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    db.commit()
    audit.record(db, admin.name, "workflow.rename", row.name, {"before": before})
    return _out(row)


@router.delete("/workflows/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: int, db: Session = Depends(get_db),
                    admin: ApiKey = Depends(require_admin)):
    row = _workflow_or_404(db, workflow_id)
    name = row.name
    db.delete(row)
    db.commit()
    audit.record(db, admin.name, "workflow.delete", name, None)


@router.get("/workflows/{workflow_id}/messages")
def list_messages(workflow_id: int, db: Session = Depends(get_db),
                  _: ApiKey = Depends(require_api_key)):
    _workflow_or_404(db, workflow_id)
    rows = db.execute(
        select(WorkflowMessage).where(WorkflowMessage.workflow_id == workflow_id)
        .order_by(WorkflowMessage.id)
    ).scalars()
    return [{"role": r.role, "content": r.content, "created_at": r.created_at} for r in rows]


@router.post("/workflows/{workflow_id}/chat")
def chat(workflow_id: int, body: WorkflowChatIn, db: Session = Depends(get_db),
         admin: ApiKey = Depends(require_admin)):
    """대화 한 번 — 스펙 제안과 **대화에서 읽어 낸 업무**를 함께 돌려준다.

    저장하지 않는다. problems가 비어 있지 않으면 화면이 저장을 막고, review는 사람이
    검토할 자리를 가리킨다(막지는 않는다 — 읽어 낸 것이 틀렸는지는 사람만 안다).
    """
    row = _workflow_or_404(db, workflow_id)
    history = [
        {"role": m.role, "content": m.content}
        for m in db.execute(
            select(WorkflowMessage).where(WorkflowMessage.workflow_id == workflow_id)
            .order_by(WorkflowMessage.id)
        ).scalars()
    ]
    try:
        result = workflowchat.propose(db, row, body.request, history)
    except workflow_service.WorkflowError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (bedrock.BedrockError, llm_service.LlmTimeout, llm_service.LlmCallFailed,
            llm_service.LlmTruncated) as e:
        # 프로바이더 쪽 사유는 **그대로 올린다.** 실측: Bedrock SSO 토큰이 만료됐을 때
        # 이걸 안 잡아서 화면에 "Internal Server Error"만 떴다 — 메시지에는 어느 프로필로
        # 재로그인하면 되는지까지 적혀 있었는데 그게 로그에만 남았다.
        raise HTTPException(status_code=502, detail=str(e))
    db.add(WorkflowMessage(workflow_id=row.id, role="user", content=body.request))
    db.add(WorkflowMessage(workflow_id=row.id, role="assistant",
                           content=result["summary"] or "(요약 없음)"))
    db.commit()
    audit.record(db, admin.name, "workflow.chat", row.name,
                 {"attempts": result["attempts"], "provider": result["provider"],
                  "problems": len(result["problems"])})
    return result


@router.post("/workflows/{workflow_id}/assessment")
def assess_workflow(workflow_id: int, db: Session = Depends(get_db),
                    admin: ApiKey = Depends(require_admin)):
    """평가 — 사람이 하는 일을 에이전트로 옮길 수 있는가, 옮기려면 무엇을 바꿔야 하는가.

    저장하지 않는다(스펙이 바뀌면 평가도 옛것이다). 결과에 change_request를 함께 주므로
    화면이 그걸 그대로 구성 대화에 넣어 **고치는 데까지** 이어갈 수 있다 — 읽고 끝나는
    평가는 아무것도 바꾸지 않는다.
    """
    row = _workflow_or_404(db, workflow_id)
    try:
        result = workflowassess.assess(db, row)
    except workflow_service.WorkflowError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (bedrock.BedrockError, llm_service.LlmTimeout, llm_service.LlmCallFailed,
            llm_service.LlmTruncated) as e:
        raise HTTPException(status_code=502, detail=str(e))
    audit.record(db, admin.name, "workflow.assess", row.name,
                 {"provider": result["provider"], **result["metrics"]})
    return {**result, "change_request": workflowassess.change_request(result)}


@router.post("/workflows/{workflow_id}/runs", status_code=201)
def start_run(workflow_id: int, db: Session = Depends(get_db),
              admin: ApiKey = Depends(require_admin)):
    row = _workflow_or_404(db, workflow_id)
    try:
        run = workflow_service.start(db, row, admin.name)
    except workflow_service.WorkflowError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(db, admin.name, "workflow.run", row.name, {"run_id": run.id})
    return _run_out(run)


@router.get("/workflows/{workflow_id}/runs")
def list_runs(workflow_id: int, limit: int = 20, db: Session = Depends(get_db),
              _: ApiKey = Depends(require_api_key)):
    _workflow_or_404(db, workflow_id)
    rows = db.execute(
        select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id)
        .order_by(WorkflowRun.id.desc()).limit(max(1, min(limit, 100)))
    ).scalars()
    return [_run_out(r) for r in rows]


@router.get("/workflows/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db),
            _: ApiKey = Depends(require_api_key)):
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="실행 기록을 찾을 수 없습니다")
    # 출력은 길어서 목록에는 싣지 않는다 — 상세에서만 본다(사람 단계의 근거가 거기 있다).
    return {**_run_out(run), "outputs": run.outputs or {}}


@router.post("/workflows/runs/{run_id}/submit")
def submit_human_step(run_id: int, body: HumanSubmit, db: Session = Depends(get_db),
                      admin: ApiKey = Depends(require_admin)):
    """사람 단계에 제출한다 — 승인하면 그 자리에서 이어 돌고, 반려하면 사유를 남기고 끝난다."""
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="실행 기록을 찾을 수 없습니다")
    node_id = run.pending_node
    try:
        workflow_service.resume(db, run, body.content, body.approved)
    except workflow_service.WorkflowError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit.record(db, admin.name, "workflow.human", f"run#{run_id}",
                 {"node": node_id, "approved": body.approved})
    return _run_out(run)


@router.post("/workflows/runs/{run_id}/cancel")
def cancel_run(run_id: int, db: Session = Depends(get_db),
               admin: ApiKey = Depends(require_admin)):
    """기다리는 실행을 접는다. 도는 중인 단계는 끝까지 가고, 그 뒤로 진행하지 않는다 —
    남의 프로세스를 중간에 끊으면 반쯤 쓴 파일이 남는다."""
    run = db.get(WorkflowRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="실행 기록을 찾을 수 없습니다")
    if run.status in (WorkflowRunStatus.succeeded, WorkflowRunStatus.failed,
                      WorkflowRunStatus.canceled):
        raise HTTPException(status_code=409, detail="이미 끝난 실행입니다")
    run.status = WorkflowRunStatus.canceled
    run.pending_node = ""
    run.error = f"{admin.name}이 취소했습니다."
    db.commit()
    audit.record(db, admin.name, "workflow.cancel", f"run#{run_id}", None)
    return _run_out(run)
