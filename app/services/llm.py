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
from . import bedrock

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


def default_provider(db: Session) -> LlmProvider | None:
    """자동으로 도는 판단(배포 점검·실패 원인·레포 검토)이 쓸 프로바이더.

    **없으면 None을 돌려준다.** 예외를 올리면 그 기능 전체가 죽는데, 그 기능들은 LLM 없이도
    돌려줄 사실(파일·로그·설정)을 이미 가지고 있다 — 사실을 주고 "기본 LLM이 없어 판단은
    붙이지 못했다"고 말하는 쪽이 낫다. 기본값이 없고 프로바이더가 하나뿐이면 그것을 쓴다:
    하나뿐인 설치본에서 "기본값 미지정"으로 멈추는 것은 설명이 아니라 실수다.
    """
    rows = list(db.execute(select(LlmProvider).order_by(LlmProvider.id)).scalars())
    for row in rows:
        if row.is_default:
            return row
    return rows[0] if len(rows) == 1 else None


def set_default(db: Session, provider: LlmProvider) -> None:
    """이 프로바이더를 기본값으로 — 하나만 참이어야 하므로 나머지를 내린다."""
    for row in db.execute(select(LlmProvider)).scalars():
        row.is_default = row.id == provider.id
    db.commit()


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


class LlmTimeout(RuntimeError):
    """응답이 제한 시간 안에 오지 않았다. 무엇을 기다렸고 어디를 고치는지 말한다."""

    def __init__(self, seconds: int, max_tokens: int):
        super().__init__(
            f"LLM이 {seconds}초 안에 응답하지 않았습니다(최대 출력 {max_tokens} 토큰). "
            "출력 한도가 크면 생성도 길어집니다 — PAAS_LLM_TIMEOUT_SECONDS를 늘리거나 "
            "PAAS_LLM_MAX_OUTPUT_TOKENS를 줄이거나, 요청 범위를 좁혀 다시 시도하세요."
        )
        self.seconds = seconds


class LlmTruncated(RuntimeError):
    """응답이 길이 제한에서 끊겼다 — 받은 부분을 함께 들고 간다.

    버리지 않는 이유: 사람이 쓴 요청과 모델이 만든 본문은 되살릴 수 없고, 잘렸다는 사실만
    알면 다시 생성하거나 범위를 좁혀 이어갈 수 있다. 조용히 완전한 것처럼 저장하는 것이
    가장 나쁘다 — 문서 끝의 C4 블록이 사라진 채 확정된다.
    """

    def __init__(self, partial: str, limit: int = 0):
        # 한도 숫자를 문구에 넣는다 — "늘리세요"만 보면 무엇에서 얼마로 올릴지 알 수 없다.
        current = f"현재 한도 {limit} 토큰" if limit > 0 else "한도를 보내지 않는 설정(0)"
        super().__init__(
            f"LLM 응답이 길이 제한에서 잘렸습니다({current}) — 문서가 문장 중간에서 "
            "끝납니다. PAAS_LLM_MAX_OUTPUT_TOKENS를 늘리거나 요청 범위를 좁혀 다시 "
            "생성하세요(모델이 허용하는 최대 출력 토큰까지 올릴 수 있습니다)."
        )
        self.partial = partial
        self.limit = limit


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
    if uses_aws_credentials(provider):
        # Bedrock 네이티브 Converse — 키가 아니라 AWS 자격증명으로 서명한다. 응답을
        # OpenAI 모양으로 되돌려 주므로 아래 tool-call 루프는 그대로 쓴다.
        data = bedrock.converse(
            profile=provider.aws_profile,
            region=bedrock.region_from_url(provider.base_url, provider.aws_profile),
            model_id=provider.model,
            messages=messages,
            tools=tools,
        )
    else:
        data = _openai_style_call(provider, messages, tools, db)
    choice = data["choices"][0]
    message = choice["message"]
    # **잘린 응답을 완전한 것처럼 돌려주지 않는다.** 예전에는 finish_reason을 아예 읽지
    # 않아서, 길이 제한에서 끊긴 산출물이 그대로 저장됐다(supplier-pool: 기획서가
    # "### 2.1 해결하려는 문제 / 구매·소싱 담당"에서 끝났다). C4 블록은 문서 끝에 두라고
    # 지시하므로 잘릴 때 가장 먼저 사라진다 — 화면에는 "시각화 미작성"으로만 보였다.
    truncated = str(choice.get("finish_reason") or "").lower() in ("length", "max_tokens")

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
    text = message.get("content") or ""
    if truncated:
        raise LlmTruncated(text, get_settings().llm_max_output_tokens)
    return text


def uses_aws_credentials(provider: LlmProvider) -> bool:
    """Bedrock을 자격증명으로 부를지. 프로필이 비어 있으면 기존 동작(api_key Bearer를
    OpenAI 호환 엔드포인트로)을 그대로 유지한다 — 앞단에 게이트웨이를 둔 설정이 있다."""
    kind_str = str(provider.kind.value if hasattr(provider.kind, "value") else provider.kind)
    return kind_str == "aws" and bool(getattr(provider, "aws_profile", None))


def _openai_style_call(
    provider: LlmProvider,
    messages: list[dict],
    tools: list[dict] | None,
    db: Session | None,
) -> dict:
    """OpenAI 호환 chat completions 경로 — 종류별 인증 헤더와 URL 형태만 다르다."""
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
    # 보내지 않으면 프로바이더 기본값이 적용되고, 그 값이 작으면 산출물이 잘린다.
    # 0은 "보내지 않는다" — 이 파라미터를 거부하는 엔드포인트용 탈출구.
    limit = get_settings().llm_max_output_tokens
    if limit > 0:
        payload["max_tokens"] = limit
    if tools:
        payload["tools"] = tools
    return _post_chat(url, headers, payload)


def _post_chat(url: str, headers: dict, payload: dict) -> dict:
    """테스트에서 monkeypatch하는 실제 HTTP 경계."""
    seconds = get_settings().llm_timeout_seconds
    try:
        res = httpx.post(url, headers=headers, json=payload, timeout=seconds)
    except httpx.TimeoutException:
        # "the read operation timed out"만 남으면 무엇을 해야 할지 알 수 없다 — 얼마를
        # 기다렸는지와 어디를 고치는지 말한다. 출력 한도를 올리면 생성이 길어져 이쪽이
        # 먼저 끊긴다(둘은 함께 움직인다).
        raise LlmTimeout(seconds, get_settings().llm_max_output_tokens)
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
