"""PaaS Central A2A (Agent-to-Agent) Gateway — A2A 규약 기반 메시지 전달 및 프로토콜 변환 전담 라우터."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ApiKey, Module, Project
from ..security import require_api_key
from ..services import a2a as a2a_service

router = APIRouter(tags=["a2a_gateway"])


@router.get("/a2a/agents")
def list_a2a_agents(
    type: str | None = None,
    category: str | None = None,
    project_id: int | None = None,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """등재된 A2A 에이전트(모듈) 카드 목록 — 이름을 미리 몰라도 찾을 수 있는 디스커버리 창구.

    project_id를 주면 그 프로젝트 관점의 조직 전용 자원까지 보이고, 없으면 전역 자원만 나온다.
    """
    project = None
    if project_id is not None:
        project = db.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")
    return a2a_service.list_agent_cards(db, project=project, type=type, category=category)


@router.get("/a2a/agents/{agent_name}/card")
def get_a2a_agent_card(
    agent_name: str,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """특정 모듈/에이전트의 A2A Agent Card (Discovery Spec) 조회."""
    module = db.execute(select(Module).where(Module.name == agent_name)).scalar_one_or_none()
    if module is None:
        raise HTTPException(status_code=404, detail=f"A2A Agent '{agent_name}' not found")
    return a2a_service.build_agent_card(module)


@router.post("/a2a/agents/{agent_name}/task")
async def execute_a2a_task(
    agent_name: str,
    request: Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """에이전트 간(Agent-to-Agent) 표준 Task 실행 요청 수신 및 PaaS 중계 실행."""
    module = db.execute(select(Module).where(Module.name == agent_name)).scalar_one_or_none()
    if module is None:
        raise HTTPException(status_code=404, detail=f"Target A2A Agent '{agent_name}' not found")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    try:
        return await asyncio.to_thread(
            a2a_service.execute_task,
            db,
            module,
            payload.get("capability") or "default",
            payload.get("input") or payload.get("params") or {},
            key.name,
            payload.get("task_id") or "a2a-task-001",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"A2A Task execution via PaaS Gateway failed: {e}")
