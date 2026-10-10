"""LLM 호출 관측 — 기록은 남고 본문은 남지 않는다, 그리고 셈은 서버가 한다."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import create_app
from app.models import LlmCallLog, LlmProvider, LlmProviderKind
from app.services import llm as llm_service
from app.services import telemetry

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


@pytest.fixture
def clean_calls():
    """기록 표를 비우고 시작한다 — 스위트가 DB 하나를 공유해서 다른 테스트의 호출이 섞인다."""
    create_app()  # 표가 없을 수 있다(create_all)
    with SessionLocal() as db:
        db.query(LlmCallLog).delete()
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(LlmCallLog).delete()
        db.commit()


def _provider() -> LlmProvider:
    return LlmProvider(name="p", kind=LlmProviderKind.openai,
                       base_url="https://api.example.com", model="m")


def _rows() -> list[LlmCallLog]:
    with SessionLocal() as db:
        return db.query(LlmCallLog).order_by(LlmCallLog.id).all()


def test_successful_call_is_recorded_without_any_prompt_text(monkeypatch, clean_calls):
    """남기는 것은 세는 값뿐이다 — 프롬프트·응답 본문이 기록에 들어가면 안 된다."""
    monkeypatch.setattr(llm_service, "_post_chat", lambda url, headers, payload: {
        "choices": [{"message": {"content": "비밀 응답"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    })
    llm_service.chat_completion(_provider(), [{"role": "user", "content": "사내 비밀 질문"}])

    row = _rows()[-1]
    assert (row.provider, row.model, row.path, row.ok) == ("p", "m", "chat.completions", True)
    assert (row.prompt_tokens, row.completion_tokens) == (11, 7)
    assert row.ms >= 0
    blob = " ".join(str(v) for v in vars(row).values())
    assert "사내 비밀 질문" not in blob and "비밀 응답" not in blob


def test_failed_call_is_recorded_with_the_reason(monkeypatch, clean_calls):
    """실패가 안 남으면 실패율을 셀 수 없다 — 예외는 그대로 올리고 기록만 더한다."""
    def boom(url, headers, payload):
        raise llm_service.LlmCallFailed(400, url, "model not found")

    monkeypatch.setattr(llm_service, "_post_chat", boom)
    with pytest.raises(llm_service.LlmCallFailed):
        llm_service.chat_completion(_provider(), [{"role": "user", "content": "x"}])

    row = _rows()[-1]
    assert row.ok is False
    assert "LlmCallFailed" in row.error and "model not found" in row.error


def test_tool_rounds_are_recorded_one_row_each(monkeypatch, clean_calls):
    """도구 왕복은 호출마다 한 행이다 — 한 턴에 몇 번 부르는지가 지연의 절반이다."""
    calls: list[dict] = []

    def fake_post_chat(url, headers, payload):
        calls.append(payload)
        if len(calls) == 1:
            return {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "function": {"name": "t", "arguments": "{}"}}],
            }}]}
        return {"choices": [{"message": {"content": "끝"}}]}

    monkeypatch.setattr(llm_service, "_post_chat", fake_post_chat)
    llm_service.chat_completion(
        _provider(), [{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "t"}}],
        tool_executor=lambda name, args: "결과",
    )
    rows = _rows()
    assert [(r.tool_round, r.tool_calls) for r in rows] == [(0, 1), (1, 0)]


def test_route_comes_from_the_request_path(clean_calls):
    """어느 화면에서 왔는지는 미들웨어가 붙인다 — 호출부 서명은 그대로다."""
    assert telemetry.normalize_route("/paas/api/v1/smartwork/sessions/12/messages") == \
        "/smartwork/sessions/:id/messages"
    with telemetry.llm_call("p", "m", "chat.completions"):
        pass
    assert _rows()[-1].route == ""  # 요청 밖(스케줄러 등)이면 빈 값


def test_summary_counts_on_the_server(clean_calls):
    """화면이 수천 행을 더하지 않는다 — 총계·모델별·일별·실패 사유를 서버가 돌려준다."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        db.add_all([
            LlmCallLog(created_at=now, provider="a", model="m1", path="chat.completions",
                       route="/smartwork/sessions/:id/messages", ok=True, ms=100,
                       prompt_tokens=10, completion_tokens=5),
            LlmCallLog(created_at=now, provider="a", model="m1", path="responses",
                       route="/smartwork/sessions/:id/messages", ok=False, ms=300,
                       error="LlmCallFailed: 400"),
            # 창 밖 — 세면 안 된다.
            LlmCallLog(created_at=now - timedelta(days=30), provider="a", model="m1",
                       ok=True, ms=1),
        ])
        db.commit()

    c = TestClient(create_app())
    out = c.get(f"{API}/telemetry/llm?days=7", headers=ADMIN).json()
    assert out["totals"]["calls"] == 2 and out["totals"]["failed"] == 1
    assert out["totals"]["prompt_tokens"] == 10 and out["totals"]["completion_tokens"] == 5
    assert out["by_model"] == [{"key": "a / m1", "calls": 2, "failed": 1,
                               "avg_ms": 200, "tokens": 15}]
    assert out["by_route"][0]["key"] == "/smartwork/sessions/:id/messages"
    assert len(out["daily"]) == 1
    assert out["failures"][0]["error"] == "LlmCallFailed: 400"
    assert out["failures"][0]["path"] == "responses"


def test_summary_is_admin_only(clean_calls):
    c = TestClient(create_app())
    assert c.get(f"{API}/telemetry/llm").status_code == 401
