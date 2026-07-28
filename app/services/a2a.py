"""A2A (Agent-to-Agent) Protocol Service — 모든 모듈 및 에이전트를 A2A Agent Entity로 정규화하고 표준 메시지를 중계한다."""
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Module, Project
from .modules import decrypt_config, binding_env


def build_agent_card(module: Module, env_prefix: str | None = None) -> dict[str, Any]:
    """모듈을 표준 A2A Agent Card (Discovery Spec) 규약으로 변환한다.
    
    실제 URL, API Key 등 민감 명세는 숨기고 비즈니스 설명(description)과
    PaaS A2A 게이트웨이 엔드포인트만 노출한다.
    """
    cfg = decrypt_config(module.config or {})
    desc = cfg.get("description") or f"A2A Autonomous Service Agent for {module.name} ({module.type.value})"
    
    capabilities = [f"capability.{module.type.value}", f"scope.{module.category or 'general'}"]
    if module.type.value == "file_storage":
        capabilities.extend(["read_file", "write_file", "list_dir"])
    elif module.type.value == "database":
        capabilities.extend(["execute_query", "inspect_schema"])
    elif module.type.value == "external_api":
        capabilities.extend(["invoke_api", "fetch_data"])

    return {
        "a2a_version": "1.0",
        "agent_name": module.name,
        "type": module.type.value,
        "category": module.category or "general",
        "description": desc,
        "capabilities": capabilities,
        "paas_a2a_endpoint": f"/paas/api/v1/a2a/agents/{module.name}/task",
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
        cards.append(build_agent_card(module, binding.env_prefix))
    return cards
