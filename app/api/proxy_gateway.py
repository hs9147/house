"""PaaS Central Proxy Gateway — 에이전트가 모듈이나 LLM을 직접 호출하지 않고 PaaS를 통하도록 중계한다."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, LlmProvider, Module
from ..security import decrypt_value, require_api_key
from ..services import llm as llm_service
from ..services import modules as modules_service

router = APIRouter(tags=["proxy_gateway"])


@router.post("/proxy/llm")
def proxy_llm_call(
    provider_id: int,
    body: dict,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """에이전트가 LLM을 직접 호출하지 않고 PaaS를 거쳐 입출력을 처리하는 게이트웨이 API."""
    provider = db.get(LlmProvider, provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="LLM provider not found")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messages field is required")

    try:
        reply = llm_service.chat_completion(provider, messages, db=db)
        audit.record(db, key.name, "proxy.llm.call", provider.name, {"model": provider.model})
        return {"reply": reply, "provider": provider.name, "model": provider.model}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PaaS LLM proxy call failed: {e}")


@router.api_route("/proxy/modules/{module_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_module_call(
    module_name: str,
    path: str,
    request: Request,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """에이전트가 외부/내부 모듈이나 API를 직접 호출하지 않고 PaaS를 거쳐 입출력을 처리하는 모듈 게이트웨이."""
    row = db.execute(select(Module).where(Module.name == module_name)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Module '{module_name}' not found")

    cfg = modules_service.decrypt_config(row.config or {})
    target_url = cfg.get("url") or cfg.get("endpoint")
    if not target_url:
        raise HTTPException(status_code=400, detail=f"Module '{module_name}' does not have a valid target endpoint URL")

    full_target_url = f"{target_url.rstrip('/')}/{path.lstrip('/')}"
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)

    api_key = cfg.get("api_key") or cfg.get("secret_key")
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key

    body_bytes = await request.body()

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.request(
                method=request.method,
                url=full_target_url,
                headers=headers,
                params=request.query_params,
                content=body_bytes,
            )
        audit.record(db, key.name, "proxy.module.call", module_name, {"status": resp.status_code, "path": path})
        return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PaaS Module proxy call failed: {e}")
