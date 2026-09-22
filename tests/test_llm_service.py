"""services/llm.py의 chat_completion — tools/tool_executor를 받으면 OpenAI 호환
tool-call 프로토콜로 모델↔도구 왕복 후 최종 텍스트만 반환한다."""
import pytest

from app.services import llm as llm_service
from app.models import LlmProvider, LlmProviderKind

TOOLS = [{"type": "function", "function": {"name": "srv__search", "description": "", "parameters": {}}}]


def _provider() -> LlmProvider:
    return LlmProvider(
        name="p", kind=LlmProviderKind.openai, base_url="https://api.example.com", model="m",
    )


def test_chat_completion_without_tools_is_unchanged(monkeypatch):
    monkeypatch.setattr(
        llm_service, "_post_chat",
        lambda url, headers, payload: {"choices": [{"message": {"content": "hi"}}]},
    )
    assert llm_service.chat_completion(_provider(), [{"role": "user", "content": "hello"}]) == "hi"


def test_chat_completion_executes_tool_call_and_loops_to_final_answer(monkeypatch):
    calls = []

    def fake_post_chat(url, headers, payload):
        calls.append(payload)
        if len(calls) == 1:
            return {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call_1", "function": {
                    "name": "srv__search", "arguments": '{"q": "weather"}',
                }}],
            }}]}
        return {"choices": [{"message": {"content": "It is sunny."}}]}

    monkeypatch.setattr(llm_service, "_post_chat", fake_post_chat)

    executed = []

    def executor(name, args):
        executed.append((name, args))
        return "sunny, 22C"

    reply = llm_service.chat_completion(
        _provider(), [{"role": "user", "content": "what's the weather?"}], tools=TOOLS, tool_executor=executor,
    )
    assert reply == "It is sunny."
    assert executed == [("srv__search", {"q": "weather"})]
    # 두 번째 호출(최종 답변 요청)의 메시지에 도구 결과가 포함돼야 한다
    second_call_messages = calls[1]["messages"]
    assert any(m.get("role") == "tool" and m.get("content") == "sunny, 22C" for m in second_call_messages)
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in second_call_messages)
    assert calls[0]["tools"] == TOOLS


def test_chat_completion_stops_after_max_rounds(monkeypatch):
    """모델이 계속 tool_calls만 요청해도 무한루프에 안 빠지고 라운드 상한에서 멈춘다."""
    call_count = {"n": 0}

    def always_tool_call(url, headers, payload):
        call_count["n"] += 1
        return {"choices": [{"message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c", "function": {"name": "srv__search", "arguments": "{}"}}],
        }}]}

    monkeypatch.setattr(llm_service, "_post_chat", always_tool_call)
    reply = llm_service.chat_completion(
        _provider(), [{"role": "user", "content": "loop please"}],
        tools=TOOLS, tool_executor=lambda name, args: "result",
    )
    assert call_count["n"] == llm_service.MAX_TOOL_ROUNDS + 1
    # 상한에 도달하면 마지막 응답의 content를 반환한다. content가 null이어도 None이
    # 아니라 빈 문자열이다 — None이 새 나가면 호출부가 문자열로 다루다 터진다.
    assert reply == ""


def test_chat_completion_without_tool_executor_ignores_tool_calls(monkeypatch):
    """tool_executor를 안 넘기면(도구 미연결) tool_calls가 와도 재귀하지 않고 그대로 반환."""
    monkeypatch.setattr(
        llm_service, "_post_chat",
        lambda url, headers, payload: {"choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": "c", "function": {"name": "x", "arguments": "{}"}}],
        }}]},
    )
    reply = llm_service.chat_completion(_provider(), [{"role": "user", "content": "hi"}], tools=TOOLS)
    assert reply == ""  # content=null → 빈 문자열(호출부가 문자열을 전제로 후처리한다)


def test_sends_the_output_token_limit(monkeypatch, fresh_settings):
    """보내지 않으면 프로바이더 기본값이 적용된다 — 실측에서 그 값이 작아 산출물이 잘렸다."""
    from app.config import get_settings
    from app.models import LlmProvider, LlmProviderKind
    from app.services import llm as llm_service

    monkeypatch.setenv("PAAS_LLM_MAX_OUTPUT_TOKENS", "4096")
    get_settings.cache_clear()
    sent = {}
    monkeypatch.setattr(llm_service, "_post_chat", lambda url, headers, payload: (
        sent.update(payload), {"choices": [{"message": {"content": "ok"}}]})[1])

    provider = LlmProvider(name="p", kind=LlmProviderKind.openai, model="m",
                           base_url="https://api.example.com")
    llm_service.chat_completion(provider, [{"role": "user", "content": "x"}])
    assert sent["max_tokens"] == 4096

    # 0은 "보내지 않는다" — 이 파라미터를 거부하는 엔드포인트용 탈출구다
    monkeypatch.setenv("PAAS_LLM_MAX_OUTPUT_TOKENS", "0")
    get_settings.cache_clear()
    sent.clear()
    llm_service.chat_completion(provider, [{"role": "user", "content": "x"}])
    assert "max_tokens" not in sent


def test_truncated_reply_is_raised_with_the_partial_text(monkeypatch, fresh_settings):
    """잘린 응답을 완전한 것처럼 돌려주면 그대로 확정된다.

    실측(supplier-pool): 기획서가 "### 2.1 해결하려는 문제 / 구매·소싱 담당"에서 끝났고
    문서 끝의 C4 블록이 사라졌다 — 화면에는 "시각화 미작성"으로만 보였다. 부분 결과는
    버리지 않는다(사람이 쓴 요청과 모델이 만든 본문을 되살릴 수 없다).
    """
    from app.models import LlmProvider, LlmProviderKind
    from app.services import llm as llm_service

    monkeypatch.setattr(llm_service, "_post_chat", lambda *a: {
        "choices": [{"message": {"content": "# 문서\n중간에서"}, "finish_reason": "length"}]})
    provider = LlmProvider(name="p", kind=LlmProviderKind.openai, model="m",
                           base_url="https://api.example.com")
    with pytest.raises(llm_service.LlmTruncated) as cut:
        llm_service.chat_completion(provider, [{"role": "user", "content": "x"}])
    assert cut.value.partial == "# 문서\n중간에서"
    assert "길이 제한" in str(cut.value)


def test_normal_finish_reason_is_not_treated_as_truncation(monkeypatch, fresh_settings):
    from app.models import LlmProvider, LlmProviderKind
    from app.services import llm as llm_service

    provider = LlmProvider(name="p", kind=LlmProviderKind.openai, model="m",
                           base_url="https://api.example.com")
    for reason in ("stop", "end_turn", None, ""):
        monkeypatch.setattr(llm_service, "_post_chat", lambda *a, r=reason: {
            "choices": [{"message": {"content": "완결"}, "finish_reason": r}]})
        assert llm_service.chat_completion(provider, [{"role": "user", "content": "x"}]) == "완결"


def test_timeout_says_how_long_it_waited_and_what_to_change(monkeypatch, fresh_settings):
    """'the read operation timed out'만 남으면 무엇을 해야 할지 알 수 없다.

    출력 한도를 올리면 생성이 길어져 이쪽이 먼저 끊긴다 — 둘은 함께 움직이므로 두 설정을
    같이 말해 준다.
    """
    import httpx

    from app.config import get_settings

    monkeypatch.setenv("PAAS_LLM_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("PAAS_LLM_MAX_OUTPUT_TOKENS", "32768")
    get_settings.cache_clear()

    def boom(*a, **kw):
        raise httpx.ReadTimeout("the read operation timed out")

    monkeypatch.setattr(llm_service.httpx, "post", boom)
    with pytest.raises(llm_service.LlmTimeout) as err:
        llm_service.chat_completion(_provider(), [{"role": "user", "content": "x"}])
    message = str(err.value)
    assert "45초" in message
    assert "32768" in message
    assert "PAAS_LLM_TIMEOUT_SECONDS" in message
    assert err.value.seconds == 45


def test_timeout_setting_is_actually_used(monkeypatch, fresh_settings):
    """고정 120초였던 자리다 — 설정이 실제로 httpx에 전달되는지 본다."""
    from app.config import get_settings

    monkeypatch.setenv("PAAS_LLM_TIMEOUT_SECONDS", "300")
    get_settings.cache_clear()
    seen = {}

    class _Res:
        status_code = 200

        def raise_for_status(self): ...

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["timeout"] = timeout
        return _Res()

    monkeypatch.setattr(llm_service.httpx, "post", fake_post)
    llm_service.chat_completion(_provider(), [{"role": "user", "content": "x"}])
    assert seen["timeout"] == 300
