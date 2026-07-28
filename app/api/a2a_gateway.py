"""PaaS Central A2A (Agent-to-Agent) Gateway — A2A 규약 기반 메시지 전달 및 프로토콜 변환 전담 라우터."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, Module
from ..security import require_api_key
from ..services import a2a as a2a_service
from ..services import modules as modules_service

router = APIRouter(tags=["a2a_gateway"])


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

    cfg = modules_service.decrypt_config(module.config or {})
    target_url = cfg.get("url") or cfg.get("endpoint")
    if not target_url:
        raise HTTPException(status_code=400, detail=f"Target A2A Agent '{agent_name}' has no endpoint URL configured")

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    task_id = payload.get("task_id") or "a2a-task-001"
    capability = payload.get("capability") or "default"
    params = payload.get("input") or payload.get("params") or {}

    headers = {
        "content-type": "application/json",
        "x-paas-a2a-gateway": "true",
        "x-paas-calling-agent": key.name,
    }

    api_key = cfg.get("api_key") or cfg.get("secret_key")
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            res = await client.post(target_url, json={"task_id": task_id, "capability": capability, "params": params}, headers=headers)

        audit.record(db, key.name, "a2a.task.execute", agent_name, {"capability": capability, "status": res.status_code})
        return {
            "jsonrpc": "2.0",
            "id": task_id,
            "result": {
                "agent_name": agent_name,
                "status": "success" if res.status_code < 400 else "failed",
                "http_code": res.status_code,
                "output": res.json() if "application/json" in res.headers.get("content-type", "") else res.text,
            }
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"A2A Task execution via PaaS Gateway failed: {e}")
