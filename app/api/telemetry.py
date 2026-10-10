"""작업 로그의 숫자 — LLM 호출 기록(services/telemetry)과 사람이 매긴 답변 평가를 센다.

**셈은 서버가 한다.** 화면이 수천 행을 받아 더하면, 같은 질문에 화면마다 다른 답이 나오고
느려진다 — 이 플랫폼의 다른 판정과 같은 원칙이다(셈과 근거는 서버가, 판정만 LLM이).

집계를 SQL의 GROUP BY로 짜지 않고 창 안의 행을 받아 파이썬에서 묶는다: 백분위(p50·p95)는
sqlite에 함수가 없어 어차피 파이썬에서 재야 하고, 한 번 읽어 한 번에 묶는 편이 날짜 함수의
DB별 차이에도 걸리지 않는다. 창은 행 수로 막는다(_MAX_ROWS).
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import AnswerRating, ApiKey, LlmCallLog
from ..security import require_admin

router = APIRouter(tags=["telemetry"])

# 한 번에 묶을 행의 상한. 넘으면 최근 것부터 자른다 — 대시보드가 메모리를 쥐고 멈추는 것보다
# 최근 구간만 정확한 편이 낫다(잘렸다는 사실은 함께 돌려준다).
_MAX_ROWS = 20000


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


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
        # 최근 실패 — 사유를 그 자리에 보여 준다(실패율만 보면 무엇을 고칠지 알 수 없다).
        "failures": [
            {"at": r.created_at.isoformat(), "model": r.model, "provider": r.provider,
             "route": r.route, "path": r.path, "error": r.error}
            for r in rows if not r.ok
        ][:20],
    }


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
