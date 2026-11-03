"""④ 같은 개체 묶기 — **형식화한 기준으로 결정론적으로** 정하고, 기준이 애매한 짝만 LLM이 판정한다.

예전에는 개념 종류마다 이름 150개를 통째로 LLM에 보내 묶음을 제안받고 묶음마다 검토했다 — 이름이 늘면 호출과
토큰이 함께 늘고, 같은 짝을 돌 때마다 다시 물었다. 이제:

  1. 강한 식별자   근거에 같은 협력사코드·사업자번호가 있으면 묶는다(실측: KR029375 = (주)엘지씨엔에스 = LG CNS
                   Co., Ltd.). 서로 다른 식별자를 가졌으면 묶지 않는다
  2. 후보 좁히기   같은 블록(이름 앞 글자·낱말·식별자)에 든 짝만 비교한다 — 모든 짝(n²)을 보지 않는다
  3. 점수         문자열 유사도(Jaro-Winkler)·낱말 겹침·음역 정규화(LG→엘지)·포함 관계를 로지스틱 점수 하나로
  4. 문턱         점수 ≥ 묶음 문턱이면 묶고, ≤ 거절 문턱이면 거절한다. **그 사이만** LLM이 짝 묶음으로 판정하고
                   검토한다(BatchER처럼 한 번에 여러 짝)
  5. 결정 캐시     짝마다 결정과 출처(id·rule·llm·human)를 남긴다 — 같은 짝을 다시 묻지 않고, 사람이 「풀기」한
                   짝은 다시 묶지 않는다

기준(식별자 패턴·음역표·떼어 낼 꼬리·점수 가중치·문턱)은 **JSON 하나**(rules)이고 판(version)이 있다. LLM은 판정한
짝에서 새 음역·약칭 규칙을 **제안만** 하고 관리자가 확정한다. 가중치·문턱은 판정이 쌓이면 다시 배운다(semantic_tune).
"""
import json
import math
import re
from datetime import datetime, timezone

DEFAULT_RULES = {
    "id_patterns": [r"\b[A-Z]{2}\d{6}\b", r"\b\d{3}-\d{2}-\d{5}\b"],   # 협력사코드(KR029375), 사업자번호
    "strip": ["주식회사", "(주)", "㈜", "(유)", "유한회사", "co.,ltd.", "co., ltd.", "co.,ltd", "co ltd", "ltd.", "ltd",
              "inc.", "inc", "corp.", "corp", "corporation", "llc", "s.l.", "srl", "gmbh"],
    "translit": {"lg": "엘지", "sk": "에스케이", "gs": "지에스", "lx": "엘엑스", "kt": "케이티", "cj": "씨제이",
                 "cns": "씨엔에스", "ls": "엘에스", "hd": "에이치디"},
    "weights": {"bias": -7.0, "jw": 6.0, "tok": 3.0, "contain": 2.0, "translit_eq": 6.0},
    "thresholds": {"merge": 0.97, "reject": 0.35},
}
LLM_BATCH = 25          # LLM에 한 번에 보내는 애매한 짝 수
SMALL_CLASS = 40        # 이름이 이만큼 이하면 블록 없이 모든 짝을 본다(짝 수가 작다)
MAX_PAIRS_PER_BLOCK = 60
_LATIN_RUN = re.compile(r"[a-z]+")    # 한 낱말 안의 로마자 덩어리(GS네오텍의 gs)
_PUNCT = re.compile(r"[\s.,\-_·()\[\]{}'\"/&]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rules_version(rules: dict) -> str:
    import hashlib  # noqa: PLC0415
    return hashlib.sha1(json.dumps(rules, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


# --- 특징 ---

def normalize(name: str, rules: dict) -> str:
    """꼬리(주식회사·Co., Ltd. 등)와 문장부호를 떼고 소문자로."""
    s = name.lower()
    for tail in sorted(rules.get("strip", []), key=len, reverse=True):
        s = s.replace(tail.lower(), " ")
    return _PUNCT.sub("", s)


def tokens(name: str, rules: dict) -> list[str]:
    s = name.lower()
    for tail in sorted(rules.get("strip", []), key=len, reverse=True):
        s = s.replace(tail.lower(), " ")
    return [t for t in _PUNCT.split(s) if t]


def transliterate(name: str, rules: dict) -> str:
    """음역 정규화 — 로마자 덩어리마다 음역표를 적용한 뒤 붙인다(LG CNS → 엘지씨엔에스, GS네오텍 → 지에스네오텍)."""
    table = {k.lower(): v for k, v in rules.get("translit", {}).items()}
    return "".join(_LATIN_RUN.sub(lambda m: table.get(m.group(), m.group()), t) for t in tokens(name, rules))


def jaro_winkler(a: str, b: str) -> float:
    if a == b:
        return 1.0 if a else 0.0
    if not a or not b:
        return 0.0
    window = max(len(a), len(b)) // 2 - 1
    hit_a, hit_b = [False] * len(a), [False] * len(b)
    matches = 0
    for i, ch in enumerate(a):
        for j in range(max(0, i - window), min(len(b), i + window + 1)):
            if not hit_b[j] and b[j] == ch:
                hit_a[i] = hit_b[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    sa = [c for c, h in zip(a, hit_a) if h]
    sb = [c for c, h in zip(b, hit_b) if h]
    transpositions = sum(x != y for x, y in zip(sa, sb)) / 2
    jaro = (matches / len(a) + matches / len(b) + (matches - transpositions) / matches) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)


def features(a: str, b: str, rules: dict) -> dict:
    na, nb = normalize(a, rules), normalize(b, rules)
    ta, tb = transliterate(a, rules), transliterate(b, rules)
    sa, sb = set(tokens(a, rules)), set(tokens(b, rules))
    shorter, longer = sorted((ta, tb), key=len)
    return {
        "jw": max(jaro_winkler(na, nb), jaro_winkler(ta, tb)),
        "tok": len(sa & sb) / len(sa | sb) if sa | sb else 0.0,
        "contain": 1.0 if len(shorter) >= 3 and shorter in longer else 0.0,
        "translit_eq": 1.0 if ta and ta == tb else 0.0,
    }


def score(feat: dict, weights: dict) -> float:
    z = weights.get("bias", 0.0) + sum(weights.get(k, 0.0) * v for k, v in feat.items())
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def ids_of(evidence: list[str], rules: dict) -> set[str]:
    found: set[str] = set()
    for pattern in rules.get("id_patterns", []):
        try:
            rx = re.compile(pattern)
        except re.error:
            continue
        for text in evidence:
            found.update(rx.findall(text or ""))
    return found


# --- 후보와 판정 ---

def candidate_pairs(names: list[str], rules: dict, ids: dict[str, set[str]]) -> set[tuple[str, str]]:
    """블록 — 같은 앞 두 글자(음역 정규화)·같은 낱말·같은 식별자를 가진 이름끼리만 짝을 만든다. 이름이 적으면 모든 짝 —
    블록으로는 표기가 다른 짝(A-Electronics·에이전자)이 만나지 못한다."""
    if len(names) <= SMALL_CLASS:
        return {tuple(sorted((a, b))) for i, a in enumerate(names) for b in names[i + 1:] if a != b}
    blocks: dict[str, list[str]] = {}
    for name in names:
        keys = {"p:" + transliterate(name, rules)[:2]} | {"t:" + t for t in tokens(name, rules) if len(t) >= 2}
        keys |= {"i:" + i for i in ids.get(name, ())}
        for k in keys:
            blocks.setdefault(k, []).append(name)
    pairs: set[tuple[str, str]] = set()
    for members in blocks.values():
        members = members[:MAX_PAIRS_PER_BLOCK]
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if a != b:
                    pairs.add(tuple(sorted((a, b))))
    return pairs


_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[a-zA-Z]")


def cross_script(a: str, b: str) -> bool:
    """한쪽은 한글, 다른 쪽은 로마자 — 글자 유사도로는 같은지 말할 수 없다(음역표에 없으면)."""
    def script(x: str) -> str:
        h, latin = len(_HANGUL.findall(x)), len(_LATIN.findall(x))
        return "ko" if h > latin else "latin" if latin > h else ""
    sa, sb = script(a), script(b)
    return bool(sa and sb and sa != sb)


def decide(a: str, b: str, rules: dict, ids: dict[str, set[str]]) -> tuple[str, str, float]:
    """(same | different | gray, 출처, 점수) — 결정론적 판정. gray만 LLM으로 간다."""
    ia, ib = ids.get(a, set()), ids.get(b, set())
    if ia & ib:
        return "same", "id", 1.0
    if ia and ib:
        return "different", "id", 0.0          # 둘 다 식별자가 있는데 겹치지 않는다
    if normalize(a, rules) == normalize(b, rules):
        return "same", "rule", 1.0
    s = score(features(a, b, rules), rules["weights"])
    if s >= rules["thresholds"]["merge"]:
        return "same", "rule", s
    if s <= rules["thresholds"]["reject"] and not cross_script(a, b):
        return "different", "rule", s
    return "gray", "rule", s         # 문턱 사이, 또는 표기가 달라 점수가 말해 주지 못하는 짝


def groups(names: dict[str, int], same: list[tuple[str, str]]) -> dict[str, str]:
    """「같다」 짝으로 묶음(union-find) → 별칭 → 대표 이름. 대표는 많은 문서에 나온 이름(같으면 짧은 것)."""
    parent = {n: n for n in names}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in same:
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    members: dict[str, list[str]] = {}
    for n in names:
        members.setdefault(find(n), []).append(n)
    out = {}
    for group in members.values():
        if len(group) < 2:
            continue
        head = max(group, key=lambda n: (names[n], -len(n), n))
        out.update({n: head for n in group if n != head})
    return out
