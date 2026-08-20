"""A2A (Agent-to-Agent) Protocol Service — 모든 모듈 및 에이전트를 A2A Agent Entity로 정규화하고 표준 메시지를 중계한다."""
import json
import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..models import Module, Project
from . import egress
from .modules import decrypt_config, binding_env

# 타입별 호출 가능한 능력(verb). capability.* / scope.* 는 분류 태그일 뿐이라 여기 넣지 않는다 —
# 도구로 노출되는 것은 이 목록뿐이다.
SKILLS_BY_TYPE = {
    "database": ["execute_query", "inspect_schema"],
    "external_api": ["invoke_api", "fetch_data"],
    "llm": ["chat_completion"],
}


def build_agent_card(module: Module, env_prefix: str | None = None) -> dict[str, Any]:
    """모듈을 표준 A2A Agent Card (Discovery Spec) 규약으로 변환한다.

    실제 URL, API Key 등 민감 명세는 숨기고 비즈니스 설명(description)과
    PaaS A2A 게이트웨이 엔드포인트만 노출한다.
    """
    cfg = decrypt_config(module.config or {})
    desc = cfg.get("description") or f"A2A Autonomous Service Agent for {module.name} ({module.type.value})"

    skills = SKILLS_BY_TYPE.get(module.type.value, ["invoke"])
    capabilities = [f"capability.{module.type.value}", f"scope.{module.category or 'general'}", *skills]

    return {
        "a2a_version": "1.0",
        "agent_name": module.name,
        "type": module.type.value,
        "category": module.category or "general",
        "description": desc,
        "capabilities": capabilities,
        "skills": skills,
        "paas_a2a_endpoint": f"/paas/api/v1/a2a/agents/{module.name}/task",
        # 임의 경로·메서드로 그대로 통과시켜야 할 때 쓰는 전송 계층 경로. 의미 단위 호출은
        # paas_a2a_endpoint 쪽이다.
        "paas_proxy_endpoint": f"/paas/api/v1/proxy/modules/{module.name}",
        "env_prefix": env_prefix or module.name.upper().replace("-", "_"),
    }


def list_project_a2a_cards(db: Session, project: Project) -> list[dict[str, Any]]:
    """프로젝트에 바인딩 및 연동된 모든 모듈을 A2A Agent Card 목록으로 추출하여 LLM 프롬프트에 주입한다."""
    from ..models import ModuleBinding  # noqa: PLC0415
    rows = db.execute(
        select(ModuleBinding, Module)
        .join(Module, ModuleBinding.module_id == Module.id)
        .where(ModuleBinding.project_id == project.id)
    ).all()

    cards = []
    for binding, module in rows:
        card = build_agent_card(module, binding.env_prefix)
        # 이 프로젝트에서 이 바인딩만 골라 해제(unbind)할 수 있어야 한다 — env_prefix로도
        # 유일하지만(프로젝트 내 unique), 콘솔이 바로 쓸 수 있는 행 식별자로 DB PK를 싣는다.
        card["binding_id"] = binding.id
        cards.append(card)
    return cards


def list_agent_cards(
    db: Session,
    project: Project | None = None,
    type: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """등재된 에이전트(모듈) 카드 목록 — 디스커버리용.

    조직 스코프는 services/modules.available_resources와 같은 규칙을 따른다: organization_id가
    없는 모듈은 전역이고, 있는 모듈은 같은 조직 소속 프로젝트 관점에서만 보인다. project를
    주지 않으면 전역 모듈만 나온다(호출자의 조직을 알 수 없으므로 좁은 쪽을 택한다).
    """
    rows = db.execute(select(Module).order_by(Module.type, Module.category, Module.name)).scalars()
    cards = []
    for module in rows:
        if module.organization_id is not None:
            if project is None or module.organization_id != project.organization_id:
                continue
        if type is not None and module.type.value != type:
            continue
        if category is not None and (module.category or "general") != category:
            continue
        cards.append(build_agent_card(module))
    return cards


def execute_task(
    db: Session,
    module: Module,
    capability: str,
    params: dict,
    caller: str,
    task_id: str = "a2a-task-001",
) -> dict[str, Any]:
    """대상 에이전트에게 Task 실행을 중계한다.

    호출자는 대상의 자격증명을 보지 못한다 — 게이트웨이가 복호화해 Authorization에 실어
    보내고, 호출자 신원은 x-paas-calling-agent로 전달한 뒤 감사 로그에 남긴다.
    대상 URL이 없으면 ValueError, 중계 자체가 실패하면 httpx 예외가 그대로 올라간다.
    """
    cfg = decrypt_config(module.config or {})
    target_url = cfg.get("url") or cfg.get("endpoint")
    if not target_url:
        raise ValueError(f"Target A2A Agent '{module.name}' has no endpoint URL configured")

    headers = {"content-type": "application/json"}
    # 호출자 신원(대개 이메일 또는 발급 키 이름)은 **사내 대상에만** 싣는다. 사외 API에
    # 붙이면 그 자체가 사내 정보 유출이고, 대상 쪽 로그에 그대로 남는다.
    if egress.is_internal_url(target_url):
        headers["x-paas-a2a-gateway"] = "true"
        headers["x-paas-calling-agent"] = caller
    api_key = cfg.get("api_key") or cfg.get("secret_key")
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    with httpx.Client(timeout=60.0) as client:
        res = client.post(
            target_url,
            json={"task_id": task_id, "capability": capability, "params": params},
            headers=headers,
        )

    audit.record(db, caller, "a2a.task.execute", module.name, {"capability": capability, "status": res.status_code})
    return {
        "jsonrpc": "2.0",
        "id": task_id,
        "result": {
            "agent_name": module.name,
            "status": "success" if res.status_code < 400 else "failed",
            "http_code": res.status_code,
            "output": res.json() if "application/json" in res.headers.get("content-type", "") else res.text,
        },
    }


def build_openai_tools(cards: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """에이전트 카드를 OpenAI 호환 tools= 스키마로 변환한다 — 카드 하나당 도구 하나.

    게이트웨이 계약(capability + input)을 그대로 노출하므로, 모델은 카드에 적힌 능력만
    고를 수 있다. 함수명은 MCP 도구(`{서버}__{도구}`)와 겹치지 않도록 a2a__ 접두사를 쓴다.
    """
    tools: list[dict] = []
    registry: dict[str, dict] = {}
    for card in cards:
        fn_name = "a2a__" + re.sub(r"[^a-zA-Z0-9_-]", "_", card["agent_name"])
        tools.append({
            "type": "function",
            "function": {
                "name": fn_name,
                "description": f"{card['description']} (A2A agent, type={card['type']})",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "capability": {
                            "type": "string",
                            "enum": card["skills"],
                            "description": "실행할 능력",
                        },
                        "input": {"type": "object", "description": "능력에 넘길 파라미터"},
                    },
                    "required": ["capability"],
                },
            },
        })
        registry[fn_name] = card
    return tools, registry


def make_tool_executor(db: Session, registry: dict[str, dict], caller: str):
    """services.llm.chat_completion에 넘길 tool_executor(name, arguments) -> str 콜백."""
    def _execute(fn_name: str, arguments: dict) -> str:
        card = registry.get(fn_name)
        if card is None:
            return f"unknown tool: {fn_name}"
        module = db.execute(select(Module).where(Module.name == card["agent_name"])).scalar_one_or_none()
        if module is None:
            return f"A2A agent '{card['agent_name']}' not found"
        try:
            result = execute_task(
                db, module, arguments.get("capability") or "default", arguments.get("input") or {}, caller,
            )
        except Exception as e:  # noqa: BLE001 — 도구 실패도 대화가 이어지도록 텍스트로 반환
            return f"a2a task failed: {e}"
        return json.dumps(result["result"], ensure_ascii=False)
    return _execute
