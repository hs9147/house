"""대화 문맥 회귀 평가 — 원장 고치기·지시어 풀기(services/convcontext.restate)를 실제 모델로 채점한다.

    python -m app.services.contexteval                 # 기본 프로바이더로
    python -m app.services.contexteval --provider 이름 --repeat 3

다중 턴 대화에서 모델이 가장 자주 틀리는 유형을 시나리오로 둔다(contexteval_cases.json): 지시어("그 표에"),
판 고치기(같은 제목의 마지막 판), 요구 분할(여러 턴에 나눠 준 조건 — LLMs Get Lost in Multi-Turn), 취소,
모호한 지시(지어내지 말고 open으로), 오래된 원장의 회상(짧게 다시 쓰다 잃지 않기 — ACE), 받지 않은 제안.
유형은 MTRAG·MultiChallenge의 다중 턴 실패 유형을 따른다. 같은 시나리오도 돌 때마다 답이 달라지므로(불안정성)
--repeat로 여러 번 돌려 통과율로 본다. 모델·프롬프트를 바꾸면 이 숫자로 전후를 견준다.

검사(expect): refers(가리켜야 할 산출물 번호 — 정확히), request_has·request_not(풀어 쓴 요청에 있어야/없어야 할
말), requirements_has·_not, decisions_has·_not, open_nonempty. 말 하나에 "A|B"는 둘 중 하나.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from . import convcontext

CASES = Path(__file__).with_name("contexteval_cases.json")


def cases() -> list[dict]:
    return json.loads(CASES.read_text(encoding="utf-8"))


def _has(text: str, word: str) -> bool:
    return any(w.strip().lower() in text.lower() for w in word.split("|"))


def score(case: dict, out: dict | None) -> list[str]:
    """틀린 검사 목록 — 비면 통과."""
    if out is None:
        return ["원장을 만들지 못함(JSON 아님·호출 실패)"]
    exp, brief, misses = case["expect"], out["brief"], []
    request = out["request"]
    joined = {k: "\n".join(brief.get(k) or []) for k in ("requirements", "decisions", "open")}
    if "refers" in exp and sorted(out["refers"]) != sorted(exp["refers"]):
        misses.append(f"refers {out['refers']} ≠ {exp['refers']}")
    for w in exp.get("request_has", []):
        if not _has(request, w):
            misses.append(f"요청에 '{w}' 없음")
    for w in exp.get("request_not", []):
        if _has(request, w):
            misses.append(f"요청에 '{w}'가 남음")
    for key in ("requirements", "decisions"):
        for w in exp.get(f"{key}_has", []):
            if not _has(joined[key], w):
                misses.append(f"{key}에 '{w}' 없음")
        for w in exp.get(f"{key}_not", []):
            if _has(joined[key], w):
                misses.append(f"{key}에 '{w}'가 있음")
    if exp.get("open_nonempty") and not brief.get("open"):
        misses.append("모호한데 open이 비었음")
    return misses


def run(ask: Callable[[str], str], repeat: int = 1, only: list[str] | None = None) -> dict:
    """ask(프롬프트) → 모델의 글. {"cases": [{id, kind, passed, runs, misses}], "by_kind", "rate"}."""
    rows = []
    for case in cases():
        if only and case["id"] not in only:
            continue
        misses_all, passed = [], 0
        for _ in range(repeat):
            misses = score(case, convcontext.restate(case["history"], ask))
            passed += not misses
            misses_all.append(misses)
        rows.append({"id": case["id"], "kind": case["kind"], "passed": passed, "runs": repeat,
                     "misses": [m for ms in misses_all for m in ms]})
    by_kind: dict[str, list[int]] = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], [0, 0])
        k[0] += r["passed"]
        k[1] += r["runs"]
    total = sum(r["runs"] for r in rows)
    return {"cases": rows, "by_kind": {k: f"{p}/{n}" for k, (p, n) in by_kind.items()},
            "rate": round(sum(r["passed"] for r in rows) / total, 3) if total else 0.0}


def main() -> None:
    from ..db import SessionLocal  # noqa: PLC0415
    from ..models import LlmProvider  # noqa: PLC0415
    from . import llm  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="대화 문맥 회귀 평가")
    parser.add_argument("--provider", default="", help="프로바이더 이름(비우면 기본값)")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--case", action="append", help="이 시나리오만(여러 번 줄 수 있다)")
    args = parser.parse_args()
    with SessionLocal() as db:
        provider = (db.query(LlmProvider).filter(LlmProvider.name == args.provider).one_or_none()
                    if args.provider else llm.default_provider(db))
        if provider is None:
            raise SystemExit("프로바이더를 찾지 못했습니다 — --provider로 이름을 주거나 기본 프로바이더를 정하세요.")
        out = run(lambda p: llm.chat_completion(provider, [{"role": "user", "content": p}], db),
                  args.repeat, args.case)
    for r in out["cases"]:
        print(f"{'ok  ' if r['passed'] == r['runs'] else 'FAIL'} {r['id']} ({r['kind']}) {r['passed']}/{r['runs']}"
              + (f" — {'; '.join(sorted(set(r['misses'])))}" if r["misses"] else ""))
    print(f"\n{provider.name} ({provider.model}) · 통과율 {out['rate']:.0%} · 유형별 {out['by_kind']}")


if __name__ == "__main__":
    main()
