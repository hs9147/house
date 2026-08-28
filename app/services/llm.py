"""LLM 프로바이더 추상화 — 외부/내부 모두 OpenAI 호환 chat completions로 호출한다.

내부 프로바이더는 base_url에 "project://<llm 프로젝트명>"을 허용하고,
호출 시점에 해당 프로젝트의 배포 도메인으로 해석한다 (소스가 사내망을 벗어나지 않음).
"""
import json
import re
from pathlib import Path
from typing import Callable

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ApiKey, BuildProfile, LlmProvider, LlmProviderKind, Project
from ..security import decrypt_value

# 플랫폼이 정한 기획·구현 원칙. 문서 하나가 원천이고, 에이전트 기획의 시스템 프롬프트에
# 주입되는 동시에 외부 빌더가 받아 가는 구현 규범이기도 하다.
AGENT_PRINCIPLES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "docs" / "agent-planning" / "AGENT.md"
)


def agent_principles_prompt() -> str:
    """기획·구현 원칙 문서를 시스템 프롬프트 조각으로 만든다. 문서가 없으면 빈 문자열."""
    try:
        text = AGENT_PRINCIPLES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return "=== 기획·구현 원칙 (플랫폼 표준 — 반드시 준수) ===\n" + text


REVIEW_SYSTEM_PROMPT = """You are a strict code reviewer. Review the given unified diff.
Reply in Korean as a JSON array of findings:
[{"severity": "high|medium|low", "file": "...", "comment": "..."}]
Return [] if the diff looks fine. Reply with JSON only."""


def require_provider_access(provider: LlmProvider, project: Project, key: ApiKey) -> None:
    """프로바이더 사용 권한은 Module과 동일한 조직 범위 규칙을 따른다.

    provider.organization_id가 없으면(NULL) 전역이라 누구나 쓸 수 있다. 지정돼 있으면
    같은 조직 소속 프로젝트에서만 쓸 수 있다 — admin은 조직 경계와 무관하게 항상 허용
    (services/modules.py available_resources의 조직 스코프 필터와 대응하는 사용 시점 검증).
    """
    if key.is_admin:
        return
    if provider.organization_id is not None and provider.organization_id != project.organization_id:
        raise HTTPException(
            status_code=403,
            detail=f"'{provider.name}' 프로바이더는 해당 조직 소속 프로젝트에서만 사용할 수 있습니다.",
        )


def resolve_base_url(base_url: str, db: Session | None = None) -> str:
    """project://name → 플랫폼에 release 프로필로 배포된 프로젝트의 실제 URL.

    1차(small)는 서브패스 기반 배포이므로(services/proxy/__init__.py의
    path_prefix_for), 대상 프로젝트의 조직에 맞는 경로를 써야 실제 배포와 일치한다.
    db가 없으면(세션을 못 넘기는 호출부) 조직을 알 수 없어 "_" 자리로 안전하게
    떨어진다 — 조직 소속 llm 프로젝트라면 가능한 경우 db를 넘길 것."""
    if not base_url.startswith("project://"):
        return base_url.rstrip("/")
    name = base_url.removeprefix("project://").strip("/")
    settings = get_settings()
    if settings.tier == "enterprise":
        return f"http://{name}.{settings.base_domain}"
    from .proxy import path_prefix_for  # noqa: PLC0415 — 순환 import 회피

    org_name = None
    if db is not None:
        target = db.execute(select(Project).where(Project.name == name)).scalar_one_or_none()
        if target is not None and target.organization is not None:
            org_name = target.organization.name
    path = path_prefix_for(org_name, name, BuildProfile.release)
    return f"http://{settings.base_domain}{path}"


MAX_TOOL_ROUNDS = 6


def chat_completion(
    provider: LlmProvider,
    messages: list[dict],
    db: Session | None = None,
    tools: list[dict] | None = None,
    tool_executor: Callable[[str, dict], str] | None = None,
    _round: int = 0,
) -> str:
    """tools/tool_executor를 주면(예: 프로젝트에 바인딩된 MCP 서버) OpenAI 호환
    tool-call 프로토콜로 모델↔도구를 오간다 — 모델이 더 이상 tool_calls를 요청하지
    않을 때까지(최대 MAX_TOOL_ROUNDS회) 반복하고 최종 텍스트만 반환한다."""
    url = resolve_base_url(provider.base_url, db)
    headers = {"content-type": "application/json"}
    
    decrypted_key = decrypt_value(provider.api_key_encrypted) if provider.api_key_encrypted else ""

    # 프로바이더(openai, anthropic, aws, azure, gcp, internal)별 인증 헤더 및 URL 구성
    kind_str = str(provider.kind.value if hasattr(provider.kind, 'value') else provider.kind)

    if kind_str == "azure":
        # Azure OpenAI Service
        if decrypted_key:
            headers["api-key"] = decrypted_key
            headers["authorization"] = f"Bearer {decrypted_key}"
        if "openai/deployments" not in url and not url.endswith("/chat/completions"):
            url = f"{url.rstrip('/')}/openai/deployments/{provider.model}/chat/completions?api-version=2024-02-15-preview"
    elif kind_str == "aws":
        # AWS Bedrock
        if decrypted_key:
            headers["authorization"] = f"Bearer {decrypted_key}"
            headers["x-api-key"] = decrypted_key
        if not url.endswith("/chat/completions") and "converse" not in url:
            url = f"{url.rstrip('/')}/v1/chat/completions"
    elif kind_str == "gcp":
        # GCP Vertex AI / Gemini API
        if decrypted_key:
            headers["authorization"] = f"Bearer {decrypted_key}"
            headers["x-goog-api-key"] = decrypted_key
        if not url.endswith("/chat/completions") and "generativelanguage" in url:
            url = f"{url.rstrip('/')}/v1beta/openai/chat/completions"
    elif kind_str == "anthropic":
        # Anthropic Official API
        if decrypted_key:
            headers["x-api-key"] = decrypted_key
            headers["anthropic-version"] = "2023-06-01"
            headers["authorization"] = f"Bearer {decrypted_key}"
        if not url.endswith("/messages") and not url.endswith("/chat/completions"):
            url = f"{url.rstrip('/')}/v1/chat/completions" if "openai" in url else f"{url.rstrip('/')}/v1/messages"
    elif kind_str == "openai":
        # OpenAI Official API
        if decrypted_key:
            headers["authorization"] = f"Bearer {decrypted_key}"
        if not url.endswith("/chat/completions"):
            url = f"{url.rstrip('/')}/v1/chat/completions" if "/v1" not in url else f"{url.rstrip('/')}/chat/completions"
    else:
        # internal (vLLM, Ollama) 사내 배포 LLM
        if decrypted_key:
            headers["authorization"] = f"Bearer {decrypted_key}"
        if not url.endswith("/chat/completions") and not url.startswith("http://127.0.0.1"):
            if not url.endswith("/v1/chat/completions"):
                url = f"{url.rstrip('/')}/v1/chat/completions"

    payload = {"model": provider.model, "messages": messages}
    if tools:
        payload["tools"] = tools
    data = _post_chat(url, headers, payload)
    message = data["choices"][0]["message"]

    tool_calls = message.get("tool_calls")
    if tool_calls and tool_executor and _round < MAX_TOOL_ROUNDS:
        next_messages = [*messages, message]
        for tc in tool_calls:
            fn = tc.get("function", {})
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            result = tool_executor(fn.get("name", ""), arguments)
            next_messages.append({
                "role": "tool", "tool_call_id": tc.get("id", ""), "content": result,
            })
        return chat_completion(provider, next_messages, db, tools, tool_executor, _round + 1)
    # content가 null인 응답(길이 초과·거절·도구 호출만 있는 경우)이 있다. 호출부가
    # 문자열을 전제로 후처리하므로 여기서 빈 문자열로 떨어뜨린다 — None이 새 나가면
    # 엉뚱한 자리에서 AttributeError로 터진다.
    return message.get("content") or ""


def _post_chat(url: str, headers: dict, payload: dict) -> dict:
    """테스트에서 monkeypatch하는 실제 HTTP 경계."""
    res = httpx.post(url, headers=headers, json=payload, timeout=120)
    res.raise_for_status()
    return res.json()


def review_diff(provider: LlmProvider, diff: str, db: Session | None = None) -> list[dict]:
    reply = chat_completion(
        provider,
        [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": f"```diff\n{diff}\n```"},
        ],
        db,
    )
    try:
        # 모델이 펜스로 감싸는 경우까지 허용
        cleaned = re.sub(r"^```(?:json)?|```$", "", reply.strip(), flags=re.MULTILINE).strip()
        findings = json.loads(cleaned)
        if isinstance(findings, list):
            return findings
    except (json.JSONDecodeError, ValueError):
        pass
    return [{"severity": "info", "file": "", "comment": reply.strip()[:2000]}]


def max_severity(findings: list[dict]) -> str:
    order = {"high": 3, "medium": 2, "low": 1}
    top = 0
    for f in findings:
        top = max(top, order.get(str(f.get("severity", "")).lower(), 0))
    return {3: "high", 2: "medium", 1: "low", 0: "none"}[top]
