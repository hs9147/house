"""기준 다시 배우기(semantic_tune) — 사람이 확인한 정답(sem_gold)으로 묶기 기준의 가중치·문턱을 다시 맞추고,
지금 기준(champion)과 새 기준(challenger)을 **배우는 데 쓰지 않은** 정답으로 견줘 나을 때만 바꾼다.

  배우기   정답의 70%(사람 판정, 무게 1) + LLM·식별자가 정한 짝(약한 정답, 무게 작게)으로 로지스틱 회귀
  문턱     묶음 문턱 = 그 위의 짝이 거의 다(PRECISION) 같은 대상인 가장 낮은 점수, 거절 문턱은 그 반대
  견주기   남긴 30%에서 기준이 스스로 정한 것(묶음·거절) 중 틀린 수, 같으면 LLM에 넘기는 짝(gray) 수

외부 ML 라이브러리 없이 — 특징이 넷뿐이라 경사 하강 몇 백 번이면 된다.
"""
import hashlib

from . import semantic_merge as sm

FEATURES = ("jw", "tok", "contain", "translit_eq")
PRECISION = 0.98       # 묶음 문턱 위 짝의 정밀도, 거절 문턱 아래 짝의 「다르다」 비율
MIN_LABELS = 20        # 사람 정답이 이보다 적으면 배우지 않는다
TEST_SHARE = 3         # 10 중 이만큼이 견주기 몫(키의 해시로 — 돌 때마다 같은 짝이 같은 쪽)
WEAK_WEIGHT = {"llm": 0.3, "id": 0.5}


def held_out(key: str) -> bool:
    return int(hashlib.sha1(key.encode()).hexdigest(), 16) % 10 < TEST_SHARE


def fit(samples: list[tuple[dict, int, float]], start: dict, l2: float = 0.01, epochs: int = 400,
        lr: float = 1.0) -> dict:
    """(특징, 정답 0·1, 무게) → 가중치. 지금 가중치에서 출발한다(정답이 적어도 크게 흔들리지 않게)."""
    w = {k: float(start.get(k, 0.0)) for k in ("bias", *FEATURES)}
    total = sum(sw for _f, _y, sw in samples) or 1.0
    for _ in range(epochs):
        grad = dict.fromkeys(w, 0.0)
        for feat, y, sw in samples:
            g = (sm.score(feat, w) - y) * sw
            grad["bias"] += g
            for k in FEATURES:
                grad[k] += g * feat.get(k, 0.0)
        for k in w:
            w[k] -= lr * (grad[k] / total + (l2 * w[k] if k != "bias" else 0.0))
    return {k: round(v, 3) for k, v in w.items()}


def thresholds(scored: list[tuple[float, int, float]], current: dict) -> dict:
    """(점수, 정답, 무게) → 문턱. 정답으로 정할 수 없으면 지금 값."""
    merge, reject = current["merge"], current["reject"]
    pts = sorted(scored, key=lambda x: x[0])        # 누적 합으로 — LLM이 정한 짝(약한 정답)이 수천 개다
    n = len(pts)
    pos_above, all_above = [0.0] * (n + 1), [0.0] * (n + 1)     # pts[i:]의 합
    for i in range(n - 1, -1, -1):
        pos_above[i] = pos_above[i + 1] + (pts[i][2] if pts[i][1] else 0.0)
        all_above[i] = all_above[i + 1] + pts[i][2]
    for i in range(n):                                # 가장 낮은 묶음 문턱
        if (i == 0 or pts[i][0] != pts[i - 1][0]) and pos_above[i] and pos_above[i] / all_above[i] >= PRECISION:
            merge = pts[i][0]
            break
    neg_below = all_below = 0.0
    below = []                                        # pts[:j]의 합
    for _s, y, w in pts:
        neg_below += 0.0 if y else w
        all_below += w
        below.append((neg_below, all_below))
    for j in range(n - 1, -1, -1):                    # 가장 높은 거절 문턱
        if (j == n - 1 or pts[j][0] != pts[j + 1][0]) and below[j][0] and below[j][0] / below[j][1] >= PRECISION:
            reject = pts[j][0]
            break
    if reject >= merge:
        return dict(current)
    return {"merge": round(merge, 4), "reject": round(reject, 4)}


def judge(rules: dict, labeled: list[tuple[str, str, int]]) -> dict:
    """기준이 스스로 정한 것 중 틀린 수와 LLM에 넘기는 수 — 식별자 없이(이름만으로 정하는 몫을 잰다)."""
    wrong = gray = 0
    for a, b, y in labeled:
        decision = sm.decide(a, b, rules, {})[0]
        if decision == "gray":
            gray += 1
        elif (decision == "same") != bool(y):
            wrong += 1
    return {"n": len(labeled), "wrong": wrong, "gray": gray}


def better(challenger: dict, champion: dict) -> bool:
    return (challenger["wrong"], challenger["gray"]) < (champion["wrong"], champion["gray"])


def tune_merge(rules: dict, human: dict[tuple[str, str], int], weak: list[tuple[str, str, int, str]]) -> dict:
    """human = {(a, b): 0·1}, weak = [(a, b, 0·1, 출처)]. → {status, reason, champion, challenger, rules?}"""
    if len(human) < MIN_LABELS:
        return {"status": "skipped", "reason": f"사람 정답 {len(human)}개 — {MIN_LABELS}개부터 배웁니다."}
    test = [(a, b, y) for (a, b), y in human.items() if held_out(f"{a}\t{b}")]
    train = [(a, b, y, 1.0) for (a, b), y in human.items() if not held_out(f"{a}\t{b}")]
    train += [(a, b, y, WEAK_WEIGHT.get(src, 0.0)) for a, b, y, src in weak
              if (a, b) not in human and WEAK_WEIGHT.get(src)]
    if len({y for _a, _b, y in test}) < 2:
        return {"status": "skipped", "reason": "견줄 정답에 「같다」·「다르다」가 다 있어야 합니다."}
    samples = [(sm.features(a, b, rules), y, w) for a, b, y, w in train]
    weights = fit(samples, rules["weights"])
    scored = [(sm.score(f, weights), y, w) for f, y, w in samples]
    challenger_rules = {**rules, "weights": weights, "thresholds": thresholds(scored, rules["thresholds"])}
    champion, challenger = judge(rules, test), judge(challenger_rules, test)
    if better(challenger, champion):
        return {"status": "promoted", "reason": "견줄 정답에서 더 낫습니다.", "champion": champion,
                "challenger": challenger, "rules": challenger_rules}
    return {"status": "kept", "reason": "새 기준이 낫지 않습니다.", "champion": champion, "challenger": challenger,
            "candidate": {"weights": weights, "thresholds": challenger_rules["thresholds"]}}
