"""LLM 호출 관측 — 기록은 남고 본문은 남지 않는다, 그리고 셈은 서버가 한다."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import create_app
from app.models import LlmCallDetail, LlmCallLog, LlmProvider, LlmProviderKind
from app.services import llm as llm_service
from app.services import telemetry

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


@pytest.fixture
def clean_calls():
    """기록 표를 비우고 시작한다 — 스위트가 DB 하나를 공유해서 다른 테스트의 호출이 섞인다."""
    create_app()  # 표가 없을 수 있다(create_all)
    with SessionLocal() as db:
        db.query(LlmCallDetail).delete()
        db.query(LlmCallLog).delete()
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(LlmCallDetail).delete()
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


def _details() -> list[LlmCallDetail]:
    with SessionLocal() as db:
        return db.query(LlmCallDetail).order_by(LlmCallDetail.call_id).all()


def test_input_is_split_into_parts_without_any_text(monkeypatch, clean_calls):
    """입력을 구간으로 나눠 실제 입력 토큰을 비율로 나눈다 — 합계는 프로바이더가 센 값, 본문은 없다."""
    monkeypatch.setattr(llm_service, "_post_chat", lambda url, headers, payload: {
        "choices": [{"message": {"content": "끝"}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 10,
                  "prompt_tokens_details": {"cached_tokens": 400}},
    })
    messages = [
        {"role": "system", "content": "당신은 사내 업무 도우미다. " * 20},
        {"role": "user", "content": "계약 정리"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "function": {"name": "docs__read_doc", "arguments": '{"path": "a.md"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "기밀 계약서 본문 " * 2000},
        {"role": "user", "content": [{"type": "text", "text": "이 그림도"},
                                     {"type": "image_url", "image_url": {"url": "data:x"}}]},
    ]
    llm_service.chat_completion(_provider(), messages,
                                tools=[{"type": "function", "function": {"name": "t"}}])
    [detail] = _details()
    parts = {(p["part"], p["name"]): p for p in detail.parts}
    assert set(parts) == {("system", ""), ("history", ""), ("tool_call", ""),
                          ("tool_result", "docs__read_doc"), ("image", ""), ("tools", "")}
    assert sum(p["tokens"] for p in detail.parts) in (999, 1000, 1001)  # 반올림
    # 도구 결과가 가장 크다 — 줄일 곳이 그것이라는 게 이 기록의 쓸모다
    assert max(detail.parts, key=lambda p: p["tokens"])["part"] == "tool_result"
    assert detail.cached_tokens == 400
    assert "기밀" not in str(detail.parts) and "도우미" not in str(detail.parts)


def test_tool_rounds_share_one_turn(monkeypatch, clean_calls):
    """도구 왕복은 한 턴이다 — 따로 부른 호출은 다른 턴이다."""
    calls: list = []

    def fake_post_chat(url, headers, payload):
        calls.append(payload)
        if len(calls) == 1:
            return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "function": {"name": "t", "arguments": "{}"}}]}}]}
        return {"choices": [{"message": {"content": "끝"}}]}

    monkeypatch.setattr(llm_service, "_post_chat", fake_post_chat)
    llm_service.chat_completion(_provider(), [{"role": "user", "content": "x"}],
                                tools=[{"type": "function", "function": {"name": "t"}}],
                                tool_executor=lambda name, args: "결과")
    llm_service.chat_completion(_provider(), [{"role": "user", "content": "y"}])
    first, second, other = (d.turn_id for d in _details())
    assert first and first == second and other and other != first


def test_summary_shows_composition_and_turns(clean_calls):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        rows = [LlmCallLog(created_at=now, provider="a", model="m1", route="/smartwork/x",
                           ok=True, ms=10, prompt_tokens=p, completion_tokens=c, tool_round=i,
                           tool_calls=t)
                for i, (p, c, t) in enumerate([(1000, 20, 2), (5000, 30, 1), (9000, 400, 0)])]
        db.add_all(rows)
        db.flush()
        for row, tool_tokens in zip(rows, (0, 4000, 8000)):
            db.add(LlmCallDetail(call_id=row.id, turn_id="turn1", parts=[
                {"part": "tools", "name": "", "chars": 100, "tokens": row.prompt_tokens - tool_tokens},
                *([{"part": "tool_result", "name": "my__read", "chars": 9, "tokens": tool_tokens}]
                  if tool_tokens else []),
            ]))
        db.commit()

    out = TestClient(create_app()).get(f"{API}/telemetry/llm?days=7", headers=ADMIN).json()
    assert out["composition"] == [{"key": "tool_result", "tokens": 12000},
                                  {"key": "tools", "tokens": 3000}]
    assert out["tool_results"] == [{"key": "my__read", "tokens": 12000}]
    [turn] = out["turns"]
    assert turn["rounds"] == 3 and turn["round_inputs"] == [1000, 5000, 9000]
    assert (turn["prompt_tokens"], turn["completion_tokens"], turn["tool_calls"]) == (15000, 450, 3)


def test_summary_compares_output_within_each_outcome_and_counts_citations(clean_calls):
    """결과 종류마다 도구 결과 입력 구간별 출력 중앙값 — 종류를 섞으면 "원래 긴 일"이 "자료를 많이
    읽어 길어진 것"으로 보인다. 인용은 출처별로 읽은 것·쓰인 것·쓰이지 않은 분량."""
    from app.models import LlmTurn

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        for i, (outcome, tool_tokens, out_tokens) in enumerate(
                [("보고서", 5_000, 800), ("보고서", 60_000, 2000), ("보고서", 70_000, 3000), ("질문", 0, 40)]):
            row = LlmCallLog(created_at=now, provider="a", model="m", route="/smartwork/x", ok=True,
                             ms=1, prompt_tokens=tool_tokens + 1000, completion_tokens=out_tokens)
            db.add(row)
            db.flush()
            db.add(LlmCallDetail(call_id=row.id, turn_id=f"t{i}", finish_reason="length" if i == 2 else "stop",
                                 parts=[{"part": "tool_result", "name": "사내 문서 · contracts",
                                         "chars": 1, "tokens": tool_tokens}],
                                 output_parts=[{"part": "tool_args", "name": "show_report", "chars": 1,
                                                "tokens": out_tokens}]))
            db.add(LlmTurn(turn_id=f"t{i}", outcome=outcome, reads=[
                {"source": "사내 문서 · contracts", "reads": 2, "cited": 1, "chars": 100,
                 "uncited_chars": 40}] if outcome == "보고서" else []))
        db.commit()

    out = TestClient(create_app()).get(f"{API}/telemetry/llm?days=7", headers=ADMIN).json()
    report = next(o for o in out["outcomes"] if o["outcome"] == "보고서")
    assert report["turns"] == 3
    assert [(b["label"], b["turns"], b["median_output"]) for b in report["bins"]] == [
        ("1만 미만", 1, 800), ("1만~5만", 0, 0), ("5만 이상", 2, 2500)]
    assert out["citations"] == [{"source": "사내 문서 · contracts", "reads": 6, "cited": 3,
                                 "chars": 300, "uncited_chars": 120}]
    assert out["output"] == [{"key": "show_report", "tokens": 5840}]
    assert out["truncated_calls"] == 1
    assert out["composition_detail"][0]["key"] == "tool_result:사내 문서 · contracts"
    assert {t["outcome"] for t in out["turns"]} == {"보고서", "질문"}
