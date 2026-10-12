"""작업 로그의 숫자 — LLM 호출 기록(services/telemetry)과 사람이 매긴 답변 평가를 센다.

**셈은 서버가 한다.** 화면이 수천 행을 받아 더하면, 같은 질문에 화면마다 다른 답이 나오고
느려진다 — 이 플랫폼의 다른 판정과 같은 원칙이다(셈과 근거는 서버가, 판정만 LLM이).

집계를 SQL의 GROUP BY로 짜지 않고 창 안의 행을 받아 파이썬에서 묶는다: 백분위(p50·p95)는
sqlite에 함수가 없어 어차피 파이썬에서 재야 하고, 한 번 읽어 한 번에 묶는 편이 날짜 함수의
DB별 차이에도 걸리지 않는다. 창은 행 수로 막는다(_MAX_ROWS).
"""
import statistics
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AnswerRating, ApiKey, LlmCallDetail, LlmCallLog, LlmTurn
from ..security import require_admin

router = APIRouter(tags=["telemetry"])

# 한 번에 묶을 행의 상한. 넘으면 최근 것부터 자른다 — 대시보드가 메모리를 쥐고 멈추는 것보다
# 최근 구간만 정확한 편이 낫다(잘렸다는 사실은 함께 돌려준다).
_MAX_ROWS = 20000
# 최근 턴 표의 행 수 — 한 턴의 왕복별 입력이 어떻게 불어나는지를 보는 자리라 많을 필요가 없다.
_RECENT_TURNS = 20
# 결과 종류별 비교의 구간 — 턴이 읽어 들인 도구 결과(입력 토큰, 추정)의 크기.
_TOOL_RESULT_BINS = ((0, 10_000, "1만 미만"), (10_000, 50_000, "1만~5만"), (50_000, None, "5만 이상"))


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _median(values: list[int]) -> int:
    """표준 중앙값(짝수면 가운데 둘의 평균) — 표본이 적은 비교에서 _pct(근접 순위)는 한쪽으로 치우친다."""
    return round(statistics.median(values)) if values else 0


def _bucket(store: dict, key: str, row) -> None:
    """키별 누적 한 칸 — 모델별·경로별 표가 같은 모양이어야 화면이 한 부품으로 그린다."""
    cell = store.setdefault(key, {"key": key, "calls": 0, "failed": 0, "ms": 0, "tokens": 0})
    cell["calls"] += 1
    cell["failed"] += 0 if row.ok else 1
    cell["ms"] += row.ms or 0
    cell["tokens"] += (row.prompt_tokens or 0) + (row.completion_tokens or 0)


def _ranked(store: dict) -> list[dict]:
    out = []
    for cell in store.values():
        out.append({"key": cell["key"], "calls": cell["calls"], "failed": cell["failed"],
                    "avg_ms": round(cell["ms"] / cell["calls"]) if cell["calls"] else 0,
                    "tokens": cell["tokens"]})
    return sorted(out, key=lambda c: c["calls"], reverse=True)


@router.get("/telemetry/llm")
def llm_summary(
    days: int = 7,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    """최근 n일의 LLM 호출 — 총계·모델별·경로별·일별, 그리고 최근 실패 사유."""
    days = max(1, min(days, 90))
    # created_at은 naive UTC로 저장된다 — 기준도 같은 모양으로 맞춘다(services/deployer와 동일).
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    rows = db.execute(
        select(LlmCallLog).where(LlmCallLog.created_at >= cutoff)
        .order_by(LlmCallLog.id.desc()).limit(_MAX_ROWS)
    ).scalars().all()

    by_model: dict = {}
    by_route: dict = {}
    by_day: dict = {}
    latencies: list[int] = []
    prompt_tokens = completion_tokens = failed = 0
    for r in rows:
        _bucket(by_model, f"{r.provider} / {r.model}", r)
        _bucket(by_route, r.route or "(요청 밖)", r)
        _bucket(by_day, r.created_at.date().isoformat(), r)
        latencies.append(r.ms or 0)
        prompt_tokens += r.prompt_tokens or 0
        completion_tokens += r.completion_tokens or 0
        failed += 0 if r.ok else 1

    return {
        "days": days,
        "truncated": len(rows) >= _MAX_ROWS,
        "totals": {
            "calls": len(rows), "failed": failed,
            "p50_ms": _pct(latencies, 0.5), "p95_ms": _pct(latencies, 0.95),
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        },
        "by_model": _ranked(by_model),
        "by_route": _ranked(by_route)[:12],
        # 일별은 시간 순으로 — 추세는 순서가 있어야 읽힌다.
        "daily": sorted(_ranked(by_day), key=lambda c: c["key"]),
        **_composition(db, rows),
        # 최근 실패 — 사유를 그 자리에 보여 준다(실패율만 보면 무엇을 고칠지 알 수 없다).
        "failures": [
            {"at": r.created_at.isoformat(), "model": r.model, "provider": r.provider,
             "route": r.route, "path": r.path, "error": r.error}
            for r in rows if not r.ok
        ][:20],
    }


def _composition(db: Session, rows: list) -> dict:
    """입력 구성(구간별 추정 토큰)·도구 결과 상위·최근 턴 — llm_call_details에서.

    구간별 몫은 추정이다(services/telemetry.composition — 구간 크기의 비율로 실제 입력 토큰을
    나눴다). 합계는 프로바이더가 센 값 그대로다. 구성 기록이 생기기 전의 호출은 빠진다.
    """
    by_id = {r.id: r for r in rows}
    details = db.execute(
        select(LlmCallDetail).where(LlmCallDetail.call_id.in_(list(by_id)))
    ).scalars().all() if by_id else []
    parts: dict[str, int] = {}
    detail: dict[str, int] = {}
    tools: dict[str, int] = {}
    output: dict[str, int] = {}
    turns: dict[str, list] = {}
    cached = truncated = 0
    for d in details:
        cached += d.cached_tokens or 0
        truncated += 1 if (d.finish_reason or "").lower() in ("length", "max_tokens") else 0
        for p in d.parts or []:
            tokens = int(p.get("tokens") or 0)
            parts[p["part"]] = parts.get(p["part"], 0) + tokens
            if p.get("name"):
                key = f"{p['part']}:{p['name']}"
                detail[key] = detail.get(key, 0) + tokens
            if p["part"] == "tool_result":
                name = p.get("name") or "(이름 없음)"
                tools[name] = tools.get(name, 0) + tokens
        for p in d.output_parts or []:
            key = "글 답" if p["part"] == "text" else (p.get("name") or "도구 인자")
            output[key] = output.get(key, 0) + int(p.get("tokens") or 0)
        if d.turn_id:
            turns.setdefault(d.turn_id, []).append((by_id[d.call_id], d))
    outcomes = {t.turn_id: t for t in db.execute(
        select(LlmTurn).where(LlmTurn.turn_id.in_(list(turns)))).scalars()} if turns else {}

    recent = []
    for tid, calls in turns.items():
        calls.sort(key=lambda c: (c[0].tool_round, c[0].id))
        first = calls[0][0]
        recent.append({
            "at": first.created_at.isoformat(), "route": first.route, "model": first.model,
            "rounds": len(calls),
            "prompt_tokens": sum(c.prompt_tokens or 0 for c, _ in calls),
            "completion_tokens": sum(c.completion_tokens or 0 for c, _ in calls),
            "cached_tokens": sum(d.cached_tokens or 0 for _, d in calls),
            # 왕복마다의 입력 — 앞의 도구 결과가 다시 실려 불어나는 모양이 이것이다.
            "round_inputs": [c.prompt_tokens or 0 for c, _ in calls],
            "tool_calls": sum(c.tool_calls or 0 for c, _ in calls),
            "outcome": outcomes[tid].outcome if tid in outcomes else "",
            # 턴이 읽어 들인 도구 결과 — 왕복마다 다시 실리므로 합이 아니라 가장 큰 왕복의 값이다.
            "tool_result_tokens": max(sum(int(p.get("tokens") or 0) for p in d.parts or []
                                          if p["part"] == "tool_result") for _, d in calls),
        })
    recent.sort(key=lambda t: t["at"], reverse=True)
    ranked = lambda store: sorted(({"key": k, "tokens": v} for k, v in store.items()),  # noqa: E731
                                  key=lambda c: c["tokens"], reverse=True)
    return {
        "composition": ranked(parts),
        "composition_detail": ranked(detail)[:15],
        "tool_results": ranked(tools)[:10],
        "output": ranked(output)[:10],
        "truncated_calls": truncated,
        "outcomes": _by_outcome(recent),
        "citations": _citations(list(outcomes.values())),
        "turns": [{k: v for k, v in t.items() if k != "tool_result_tokens"}
                  for t in recent[:_RECENT_TURNS]],
        "cached_tokens": cached,
    }


def _by_outcome(turns: list[dict]) -> list[dict]:
    """결과 종류별로, 읽어 들인 도구 결과의 크기 구간마다 출력 중앙값 — **비교이지 인과가 아니다.**

    결과 종류(보고서·에이전트·질문…)가 요청 종류를 대신한다. 같은 종류 안에서 도구 결과가
    많을수록 출력이 길어지는지를 보는 자리다 — 종류를 섞으면 "보고서 요청은 원래 길다"가
    "자료를 많이 읽으면 길어진다"로 보인다. 중앙값을 쓴다(긴 꼬리 하나가 평균을 끌고 간다).
    """
    groups: dict[str, list[dict]] = {}
    for t in turns:
        groups.setdefault(t["outcome"] or "(기록 없음)", []).append(t)
    out = []
    for outcome, rows in groups.items():
        bins = []
        for low, high, label in _TOOL_RESULT_BINS:
            inside = [r for r in rows if r["tool_result_tokens"] >= low
                      and (high is None or r["tool_result_tokens"] < high)]
            bins.append({"label": label, "turns": len(inside),
                         "median_output": _median([r["completion_tokens"] for r in inside])})
        out.append({"outcome": outcome, "turns": len(rows),
                    "median_input": _median([r["prompt_tokens"] for r in rows]),
                    "median_output": _median([r["completion_tokens"] for r in rows]),
                    "bins": bins})
    return sorted(out, key=lambda o: o["turns"], reverse=True)


def _citations(turns: list) -> list[dict]:
    """읽은 자료가 결과에 쓰였는지 출처별로 — 읽고 쓰지 않은 분량은 입력만 불린 것이다."""
    by: dict[str, dict] = {}
    for t in turns:
        for r in t.reads or []:
            cell = by.setdefault(r["source"], {"source": r["source"], "reads": 0, "cited": 0,
                                               "chars": 0, "uncited_chars": 0})
            for k in ("reads", "cited", "chars", "uncited_chars"):
                cell[k] += int(r.get(k) or 0)
    return sorted(by.values(), key=lambda c: c["chars"], reverse=True)


@router.get("/telemetry/quality")
def quality_summary(
    days: int = 30,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    """사람이 매긴 답변 평가 — 모델별 좋음·아쉬움과 그 사유(answer_ratings).

    세 가지를 한 화면에서 같이 봐야 뜻이 된다: **표본**(몇 건 매겼나), **비율**(좋음 몇 %),
    **사유**(왜 아쉬웠나). 비율만으로 줄 세우면 2건 중 2건 좋음인 모델이 1위가 되므로 건수를
    함께 돌려준다 — 적은 표본으로 모델을 바꾸는 결정을 하지 않게.

    **누가 매겼는지는 내보내지 않는다**(사람 수만 센다). 이름이 보이면 솔직한 평가가 줄고,
    그러면 측정 자체가 망가진다.
    """
    days = max(1, min(days, 365))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    rows = db.execute(
        select(AnswerRating).where(AnswerRating.created_at >= cutoff)
        .order_by(AnswerRating.id.desc()).limit(_MAX_ROWS)
    ).scalars().all()

    by_model: dict = {}
    raters: set = set()
    good = 0
    for r in rows:
        key = f"{r.provider or '?'} / {r.model or '?'}"
        cell = by_model.setdefault(key, {"key": key, "rated": 0, "good": 0, "poor": 0})
        cell["rated"] += 1
        cell["good" if r.score > 0 else "poor"] += 1
        good += 1 if r.score > 0 else 0
        raters.add(r.rater)

    return {
        "days": days,
        "totals": {"rated": len(rows), "good": good, "poor": len(rows) - good,
                   "raters": len(raters)},
        "by_model": sorted(by_model.values(), key=lambda c: c["rated"], reverse=True),
        # 아쉬움의 사유만 모은다 — 고칠 거리가 거기 있다(좋음에는 보통 아무 말도 안 적는다).
        "recent_poor": [
            {"at": r.created_at.isoformat(), "model": r.model, "provider": r.provider,
             "reason": r.reason}
            for r in rows if r.score < 0 and r.reason
        ][:20],
    }
