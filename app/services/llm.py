"""LLM 프로바이더 추상화 — 외부/내부 모두 OpenAI 호환 chat completions로 호출한다.

내부 프로바이더는 base_url에 "project://<llm 프로젝트명>"을 허용하고,
호출 시점에 해당 프로젝트의 배포 도메인으로 해석한다 (소스가 사내망을 벗어나지 않음).
"""
import json
import re
import time
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


class LlmCallFailed(RuntimeError):
    """프로바이더가 오류로 답했다 — **본문을 함께 싣는다.**

    httpx의 raise_for_status는 상태 코드와 URL만 남긴다. 400의 이유는 본문에만 있는데
    (어느 파라미터가 거부됐는지, 배포 이름이 틀렸는지, 한도를 넘었는지) 그것을 버리면 화면에
    "400 Bad Request"만 남고 사람은 추측부터 시작한다 — 실측에서 Azure 프로바이더가 그렇게
    이유 없이 죽었다. 본문은 길 수 있으므로 앞부분만 싣는다.
    """

    def __init__(self, status: int, url: str, body: str):
        super().__init__(
            f"LLM 프로바이더가 HTTP {status}로 답했습니다 — {body.strip()[:600] or '(본문 없음)'}"
        )
        self.status = status
        self.url = url
        self.body = body


class NeedsResponsesApi(LlmCallFailed):
    """이 모델은 chat/completions에서 도구를 쓸 수 없다 — /v1/responses로 가야 한다.

    LlmCallFailed를 상속한다: 응답 경로까지 실패하면 호출부(provider_error 등)가 지금처럼
    502로 올리면 되고, 새 except 절을 온 코드에 뿌릴 필요가 없다.
    """


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


# 점검용 도구 하나 — **도구를 붙이는 것이 요점이다.** 대화는 늘 MCP 도구를 함께 보내는데,
# 도구 없는 호출만 받는 모델이 있다(추론 모델이 tools와 부딪힌다 — _post_chat 참조).
# 도구 없이 점검하면 "통과"라고 말한 뒤 대화에서만 터진다.
_CHECK_TOOL = [{
    "type": "function",
    "function": {"name": "paas_ping", "description": "점검용 — 호출하지 마세요.",
                 "parameters": {"type": "object", "properties": {}}},
}]


def check(provider: LlmProvider, db: Session | None = None) -> dict:
    """프로바이더를 실제로 한 번 불러 본다 — 걸린 시간과 고쳐 보낸 내역을 함께 돌려준다.

    실패를 삼키지 않는다: 예외를 그대로 올려 호출부가 provider_error로 바꾼다(SSO 만료면
    그 자리에서 서버 로그인이 시작되고 사람 화면에 승인 주소가 뜬다).
    """
    started = time.monotonic()
    reply = chat_completion(
        provider,
        [{"role": "user", "content": "연결 점검입니다. 도구를 쓰지 말고 OK 라고만 답하세요."}],
        db, tools=_CHECK_TOOL,
    )
    return {"ok": True, "elapsed_ms": int((time.monotonic() - started) * 1000),
            "reply": reply.strip()[:200]}


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
    try:
        return _post_chat(url, headers, payload)
    except NeedsResponsesApi as e:
        # 모델이 직접 알려 준 길로 간다. 설정을 받지 않는 이유: 모델 이름 표를 두면 새
        # 모델이 나올 때마다 뒤처진다(max_tokens 재시도와 같은 판단).
        return _responses_call(url, headers, payload, e)


def _post_chat(url: str, headers: dict, payload: dict) -> dict:
    """테스트에서 monkeypatch하는 실제 HTTP 경계."""
    seconds = get_settings().llm_timeout_seconds
    limit = payload.get("max_tokens")

    def send(body: dict):
        return httpx.post(url, headers=headers, json=body, timeout=seconds)

    try:
        res = send(payload)
    except httpx.TimeoutException:
        # "the read operation timed out"만 남으면 무엇을 해야 할지 알 수 없다 — 얼마를
        # 기다렸는지와 어디를 고치는지 말한다. 출력 한도를 올리면 생성이 길어져 이쪽이
        # 먼저 끊긴다(둘은 함께 움직인다).
        raise LlmTimeout(seconds, get_settings().llm_max_output_tokens)
    # **`max_tokens`를 거부하는 엔드포인트가 있다.** Azure OpenAI의 최신 모델과 OpenAI의
    # 추론 모델은 `max_completion_tokens`만 받고, 옛 이름에는 400으로 답한다. 실측에서
    # 기본 프로바이더(Azure gpt-5.2)가 그 400으로 죽었고, 화면에는 이유 없는
    # "400 Bad Request"만 남았다. 이름만 바꿔 한 번 더 보낸다 — 프로바이더마다 표를 만들면
    # 모델이 새로 나올 때마다 그 표가 뒤처진다.
    if res.status_code == 400 and "max_tokens" in payload:
        body = res.text[:500]
        if "max_completion_tokens" in body or "max_tokens" in body:
            payload = {k: v for k, v in payload.items() if k != "max_tokens"}
            payload["max_completion_tokens"] = limit
            res = send(payload)
    # **추론 모델은 chat/completions에서 도구와 추론을 함께 못 쓴다.** 우리는
    # reasoning_effort를 보내지 않지만, 모델에 서버 기본값이 걸려 있으면 tools와 부딪혀
    # 400이 온다(실측: gpt-6.1-sol). 본문이 알려 주는 대로 'none'을 명시해 한 번 더 보낸다 —
    # 도구를 쓰는 대화가 추론 설정 때문에 아예 못 돌아가는 것보다 낫다.
    if res.status_code == 400 and "reasoning_effort" in res.text[:500] \
            and payload.get("reasoning_effort") != "none":
        conflict = res.text.strip()[:400]
        payload = {**payload, "reasoning_effort": "none"}
        res = send(payload)
        # **'none'까지 거부하는 모델이 있다** — 지원값이 low·medium·high·xhigh뿐이라고
        # 답한다(실측). 앞 400은 "none으로 두라"고 했으니 두 답이 서로 모순이고, 그러면
        # 이 모델로는 chat/completions에서 도구를 쓸 길이 없다. 앞 400이 알려 준 다른 길
        # (/v1/responses)로 가라고 전용 예외를 던진다 — 두 사유를 함께 싣는다.
        if res.status_code == 400 and "reasoning_effort" in res.text[:500]:
            raise NeedsResponsesApi(400, url, (
                f"{conflict} / reasoning_effort=none으로 다시 보냈더니: "
                f"{res.text.strip()[:300]}"))
    if res.status_code >= 400:
        # **본문을 싣는다.** 400의 이유는 본문에만 있다(어느 파라미터가 문제인지, 배포
        # 이름이 틀렸는지). 그것을 버리면 화면에 남는 것은 상태 코드뿐이고, 그러면 사람은
        # 추측부터 시작한다.
        raise LlmCallFailed(res.status_code, url, res.text)
    return res.json()


# --- 응답(Responses) API ---
#
# 추론 모델 일부는 chat/completions에서 **도구를 아예 받지 않는다**(_post_chat 참조).
# 그 모델이 알려 주는 다른 길이 /v1/responses다. 요청·응답 모양이 달라서, 경계에서
# 번역만 하고 **위쪽 도구 루프는 그대로 쓴다** — chat_completion이 두 가지 모양을 알게
# 되면 분기가 온 함수로 번진다.
#
# 번역하는 것(요청): messages → input, 중첩 tools → 평평한 tools, max_tokens →
# max_output_tokens. 번역하는 것(응답): output 항목들 → choices[0].message.
# 추론 설정(reasoning)은 **보내지 않는다** — 모델 기본값에 맡긴다. 그걸 건드리려다
# 막다른 길에 들어선 것이다.


def _responses_url(url: str) -> str:
    return url.replace("/chat/completions", "/responses")


def _responses_input(messages: list[dict]) -> list[dict]:
    """chat 모양의 대화를 응답 API의 input 항목으로 옮긴다.

    도구 왕복이 핵심이다: chat은 assistant.tool_calls와 role="tool" 메시지로 짝을 짓고,
    응답 API는 function_call·function_call_output 항목으로 짓는다. call_id로 이어지므로
    그 값을 잃으면 모델이 어느 호출의 결과인지 모른다.
    """
    items: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "tool":
            items.append({"type": "function_call_output",
                          "call_id": m.get("tool_call_id", ""), "output": str(content)})
            continue
        if role == "assistant":
            if content:
                items.append({"role": "assistant", "content": str(content)})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                items.append({"type": "function_call", "call_id": tc.get("id", ""),
                              "name": fn.get("name", ""), "arguments": fn.get("arguments") or "{}"})
            continue
        items.append({"role": role or "user", "content": str(content)})
    return items


def _responses_tools(tools: list[dict]) -> list[dict]:
    """중첩({"function": {...}})을 평평하게 — 응답 API는 한 겹으로 받는다."""
    flat: list[dict] = []
    for t in tools:
        fn = t.get("function", t)
        flat.append({"type": "function", "name": fn.get("name", ""),
                     "description": fn.get("description", ""),
                     "parameters": fn.get("parameters") or {"type": "object", "properties": {}}})
    return flat


def _responses_payload(payload: dict) -> dict:
    out: dict = {"model": payload["model"], "input": _responses_input(payload["messages"])}
    limit = payload.get("max_tokens") or payload.get("max_completion_tokens")
    if limit:
        out["max_output_tokens"] = limit
    if payload.get("tools"):
        out["tools"] = _responses_tools(payload["tools"])
    return out


def _responses_to_choice(data: dict) -> dict:
    """응답 API의 output을 chat completions 모양으로 되돌린다."""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for item in data.get("output") or []:
        kind = item.get("type")
        if kind == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id", ""),
                "type": "function",
                "function": {"name": item.get("name", ""),
                             "arguments": item.get("arguments") or "{}"},
            })
        elif kind == "message":
            for part in item.get("content") or []:
                if part.get("type") == "output_text":
                    text_parts.append(part.get("text") or "")
        # type="reasoning"은 버린다 — 요약이 올 수 있지만 산출물이 아니다.
    # 길이에서 끊긴 것을 완전한 것처럼 돌려주지 않는다(chat_completion이 finish_reason을 본다).
    incomplete = str((data.get("incomplete_details") or {}).get("reason") or "")
    if incomplete == "max_output_tokens":
        reason = "length"
    elif tool_calls:
        reason = "tool_calls"
    else:
        reason = "stop"
    message: dict = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": reason}]}


def _responses_call(url: str, headers: dict, payload: dict, refused: NeedsResponsesApi) -> dict:
    """응답 API로 한 번 보낸다. 여기서도 막히면 **두 경로의 사유를 함께** 올린다."""
    target = _responses_url(url)
    seconds = get_settings().llm_timeout_seconds
    try:
        res = httpx.post(target, headers=headers, json=_responses_payload(payload),
                         timeout=seconds)
    except httpx.TimeoutException:
        raise LlmTimeout(seconds, get_settings().llm_max_output_tokens)
    if res.status_code >= 400:
        # chat/completions의 사유를 버리지 않는다 — "응답 API도 400"만 남으면 애초에
        # 왜 여기로 왔는지가 사라져, 사람은 모델 설정부터 다시 추측한다.
        raise LlmCallFailed(res.status_code, target, (
            f"{res.text.strip()[:400]} (chat/completions가 도구를 거부해 응답 API로 "
            f"옮겨 온 것입니다 — 먼저 받은 사유: {refused.body.strip()[:300]})"))
    return _responses_to_choice(res.json())


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
