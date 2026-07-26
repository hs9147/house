"""services/llm.py의 chat_completion — tools/tool_executor를 받으면 OpenAI 호환
tool-call 프로토콜로 모델↔도구 왕복 후 최종 텍스트만 반환한다."""
from app.services import llm as llm_service
from app.models import LlmProvider, LlmProviderKind

TOOLS = [{"type": "function", "function": {"name": "srv__search", "description": "", "parameters": {}}}]


def _provider() -> LlmProvider:
    return LlmProvider(
        name="p", kind=LlmProviderKind.external, base_url="https://api.example.com", model="m",
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
    # 상한에 도달하면 마지막 응답의 content(None)를 그대로 반환한다 — 호출부가 처리
    assert reply is None


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
    assert reply is None
