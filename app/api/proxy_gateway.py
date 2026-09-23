"""PaaS Central Proxy Gateway — 에이전트가 모듈이나 LLM을 직접 호출하지 않고 PaaS를 통하도록 중계한다."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, LlmProvider, Module
from ..security import decrypt_value, require_agent_key
from ..services import egress
from ..services import llm as llm_service
from ..services import modules as modules_service

router = APIRouter(tags=["proxy_gateway"])


@router.post("/proxy/llm")
def proxy_llm_call(
    provider_id: int,
    body: dict,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_agent_key),
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


_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH"]


# 경로 없는 형태도 **직접** 받는다. `{path:path}` 하나만 두면 `/proxy/modules/{이름}`은
# FastAPI의 redirect_slashes가 307로 돌려보내는데, POST에서 그 리다이렉트를 따라가며 본문을
# 흘리는 클라이언트가 있다(MCP는 전부 POST + JSON-RPC 본문이다). 두 경로가 같은 함수를 쓰므로
# 동작이 갈릴 일은 없다.
@router.api_route("/proxy/modules/{module_name}", methods=_PROXY_METHODS)
@router.api_route("/proxy/modules/{module_name}/{path:path}", methods=_PROXY_METHODS)
async def proxy_module_call(
    module_name: str,
    request: Request,
    path: str = "",
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_agent_key),
):
    """에이전트가 외부/내부 모듈이나 API를 직접 호출하지 않고 PaaS를 거쳐 입출력을 처리하는 모듈 게이트웨이."""
    row = db.execute(select(Module).where(Module.name == module_name)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Module '{module_name}' not found")

    cfg = modules_service.decrypt_config(row.config or {})
    target_url = cfg.get("url") or cfg.get("endpoint")
    if not target_url:
        raise HTTPException(status_code=400, detail=f"Module '{module_name}' does not have a valid target endpoint URL")

    # **경로가 비면 슬래시를 붙이지 않는다.** `{path:path}`는 `/proxy/modules/{이름}`으로
    # 부를 때 빈 문자열이고, 그때 f-문자열로 이어 붙이면 대상 주소가 `…/mcp/docs/`가 된다.
    # 사내 MCP 서버는 그 주소를 다른 자원으로 보고 404·405로 답한다 — 모듈 점검
    # (modules의 mcp-check → mcp_client.check_server)은 모듈 URL을 **그대로** 불러 정상인데
    # 게이트웨이 경유만 깨지던 이유가 이것이다. 경로가 있을 때만 이어 붙인다.
    full_target_url = target_url.rstrip("/") + (f"/{path.lstrip('/')}" if path else "")
    # 들어온 헤더를 통째로 넘기면 **호출자의 자격증명이 대상에게 그대로 나간다** —
    # x-api-key(플랫폼 키·세션 토큰), cookie(로그인 세션), authorization(OIDC 토큰).
    # 대상이 사외 API면 그게 곧 사내 정보 유출이다. 그래서 허용 목록만 넘긴다.
    headers = egress.forward_headers(request.headers)

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
