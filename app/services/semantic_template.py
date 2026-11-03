"""③′ 템플릿 문서 — 같은 틀로 되풀이되는 문서는 LLM이 문서마다 읽지 않고, **LLM이 짠 추출 규칙(JSON)**으로 뽑는다.

양식(②)은 머리글이 같은 표다. 템플릿은 표가 아니어도 틀이 같은 문서 — 실측: 「신규 협력사 등록」 pptx 열 건은 제목이
하나도 없지만 「기업명 / 사업자 등록 번호 / 거래대상 / 1. 기업 신용 평가 결과」 같은 줄이 모두 같고 값만 다르다.

  1. 틀 찾기     문서마다 짧은 줄(숫자는 #로)의 집합을 만들고, 겹침(Jaccard)이 큰 문서끼리 묶는다. 묶음의 대부분에
                 있는 줄이 뼈대(skeleton)다 — 뼈대를 많이 가진 문서가 그 템플릿이다
  2. 규칙 짜기   LLM이 표본 문서와 그 문서에서 LLM이 뽑은 항목(정답)을 보고 정규식 규칙을 짠다
  3. 검증       규칙을 **다른** 문서들에 돌려 LLM 추출과 견준다 — 정밀도가 기준을 넘어야 확정할 수 있다
  4. 적용       확정한 규칙으로 뽑는다. 꼭 있어야 할 필드(required)를 못 찾으면 그 문서는 LLM으로 넘긴다

LLM이 쓴 정규식을 서버가 돌린다 — 길이를 제한하고, 글은 문서 앞부분(DOC_CHARS)만 본다.
"""
import hashlib
import json
import re

from . import semantic_merge as sm

LINE_CHARS = 6_000          # 틀을 볼 때 읽는 앞부분
LINE_MIN, LINE_MAX = 2, 40  # 뼈대 후보 줄의 길이(공백 뺀 글자 수) — 긴 줄은 내용이다
JOIN_SIMILARITY = 0.4       # 묶음 대표와 이만큼 겹치면 같은 틀
MEMBER_COVERAGE = 0.7       # 뼈대 줄을 이만큼 가지면 그 템플릿의 문서
SKELETON_SHARE = 0.8        # 묶음 문서의 이만큼에 있는 줄이 뼈대
SKELETON_MAX = 60
SKELETON_MIN = 5            # 뼈대가 이보다 적으면 틀이라 하지 않는다
COMMON_POSTING = 200        # 이보다 많은 대표에 있는 줄은 후보를 고르는 데 쓰지 않는다(너무 흔하다)
PATTERN_MAX = 300
MATCHES_PER_FIELD = 50
_ONLY_SYMBOLS = re.compile(r"[#|\-:.,·()\[\]/*~]+")


def lines_of(body: str) -> set[str]:
    """틀을 보는 줄 — 공백을 빼고 숫자는 #로(날짜·금액은 문서마다 다르다). 기호뿐인 줄(표 구분선)은 뺀다."""
    out: set[str] = set()
    for line in body[:LINE_CHARS].splitlines():
        s = re.sub(r"\d", "#", re.sub(r"\s+", "", line))
        if LINE_MIN <= len(s) <= LINE_MAX and not _ONLY_SYMBOLS.fullmatch(s):
            out.add(s)
    return out


def coverage(lines: set[str], skeleton: list[str]) -> float:
    return sum(1 for s in skeleton if s in lines) / len(skeleton) if skeleton else 0.0


def skeleton_of(members: list[set[str]]) -> list[str]:
    counts: dict[str, int] = {}
    for lines in members:
        for s in lines:
            counts[s] = counts.get(s, 0) + 1
    need = max(2, SKELETON_SHARE * len(members))
    common = sorted((s for s, n in counts.items() if n >= need), key=lambda s: (-counts[s], s))
    return sorted(common[:SKELETON_MAX])


def template_id(skeleton: list[str]) -> str:
    return hashlib.sha1("\n".join(skeleton).encode()).hexdigest()[:12]


def member_of(lines: set[str], saved: dict[str, list[str]]) -> str | None:
    """이미 아는 템플릿(뼈대) 중 가장 잘 맞는 것 — 기준을 넘을 때만."""
    best, best_cov = None, MEMBER_COVERAGE
    for tid, skeleton in saved.items():
        cov = coverage(lines, skeleton)
        if cov >= best_cov:
            best, best_cov = tid, cov
    return best


def cluster(docs: dict[str, set[str]], saved: dict[str, list[str]], min_docs: int) -> dict[str, dict]:
    """템플릿 → {skeleton, paths}. 아는 템플릿(saved — 제안·확정한 것)에 먼저 넣고, 남은 문서끼리 새 틀을 찾는다.
    대표 하나와만 견주고(문서 쌍 전부가 아니다), 대표 후보는 겹치는 줄로 고른다(줄 → 대표 색인)."""
    out = {tid: {"skeleton": sk, "paths": []} for tid, sk in saved.items()}
    rest = []
    for path in sorted(docs):
        tid = member_of(docs[path], saved)
        if tid:
            out[tid]["paths"].append(path)
        else:
            rest.append(path)
    leaders: list[str] = []
    posting: dict[str, list[int]] = {}
    groups: list[list[str]] = []
    for path in rest:
        lines = docs[path]
        if len(lines) < SKELETON_MIN:
            continue
        votes: dict[int, int] = {}
        for s in lines:
            hits = posting.get(s, ())
            if len(hits) <= COMMON_POSTING:
                for k in hits:
                    votes[k] = votes.get(k, 0) + 1
        best, best_sim = -1, JOIN_SIMILARITY
        for k in sorted(votes, key=lambda k: -votes[k])[:5]:
            other = docs[leaders[k]]
            sim = len(lines & other) / len(lines | other)
            if sim >= best_sim:
                best, best_sim = k, sim
        if best >= 0:
            groups[best].append(path)
            continue
        leaders.append(path)
        groups.append([path])
        for s in lines:
            posting.setdefault(s, []).append(len(leaders) - 1)
    for paths in groups:
        if len(paths) < min_docs:
            continue
        skeleton = skeleton_of([docs[p] for p in paths])
        if len(skeleton) < SKELETON_MIN:
            continue
        tid = template_id(skeleton)
        if tid not in out:
            out[tid] = {"skeleton": skeleton, "paths": [p for p in paths if coverage(docs[p], skeleton) >= MEMBER_COVERAGE]}
    return out


# --- 규칙 ---
# {"fields":[{"class":"협력사","pattern":"^기업명\\n(.+)$","required":true}], "relations":[{"from":0,"rel":"","to":1}]}
# 필드는 문서에서 그 개념의 이름을 찾는다(첫 괄호 묶음이 이름, 없으면 맞은 글 전체). 관계는 두 필드에서 찾은 이름을
# 잇는다 — 템플릿 문서는 대개 주인공이 하나(협력사)라 그것과 나머지를 잇는 꼴이다.

def check_rules(rules: dict, schema: dict) -> list[str]:
    classes = {c["name"] for c in schema["classes"]}
    rels = {(r["name"], r["from"], r["to"]) for r in schema["relations"]}
    fields = rules.get("fields")
    if not isinstance(fields, list) or not fields:
        return ["필드(fields)가 없습니다."]
    problems = []
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            problems.append(f"필드 {i}가 객체가 아닙니다.")
            continue
        if f.get("class") not in classes:
            problems.append(f"필드 {i}의 개념 '{f.get('class')}'가 스키마에 없습니다.")
        pattern = str(f.get("pattern", ""))
        if not pattern or len(pattern) > PATTERN_MAX:
            problems.append(f"필드 {i}의 정규식이 비었거나 {PATTERN_MAX}자를 넘습니다.")
            continue
        try:
            re.compile(pattern, re.M)
        except re.error as e:
            problems.append(f"필드 {i}의 정규식이 틀렸습니다: {e}")
    for r in rules.get("relations") or []:
        a, b = r.get("from"), r.get("to")
        if not (isinstance(a, int) and isinstance(b, int) and 0 <= a < len(fields) and 0 <= b < len(fields)):
            problems.append(f"관계 {r.get('rel')}의 from·to가 필드 번호가 아닙니다.")
            continue
        key = (r.get("rel"), (fields[a] or {}).get("class"), (fields[b] or {}).get("class"))
        if key not in rels:
            problems.append(f"관계 {key[1]} —{key[0]}→ {key[2]}가 스키마에 없습니다.")
    return problems


def _line_at(text: str, start: int, end: int, name: str) -> str:
    """근거 — 맞은 곳이 든 줄(들). 그 줄이 이름뿐이면 앞 줄(라벨 — 「기업명」)까지."""
    a = text.rfind("\n", 0, start) + 1
    b = text.find("\n", end)
    line = re.sub(r"\s+", " ", text[a:b if b >= 0 else len(text)]).strip()
    if line == name and a > 0:
        a = text.rfind("\n", 0, a - 1) + 1
        line = re.sub(r"\s+", " ", text[a:b if b >= 0 else len(text)]).strip()
    return line[:400]


def plain(text: str) -> str:
    """규칙이 보는 글 — 빈 줄을 뺀다. 마크다운으로 옮긴 문서는 줄마다 빈 줄이 끼어 있어(문단) 「라벨 다음 줄」이
    두 줄 아래가 된다(실측). LLM이 규칙을 짤 때도 이 글을 본다."""
    return re.sub(r"\n\s*\n+", "\n", text.replace("\r", ""))


def apply_rules(rules: dict, text: str) -> tuple[list[dict], list[dict], list[int]]:
    """(개체, 관계, 못 찾은 required 필드 번호). 근거는 문서의 줄이라 기계 검사(근거가 원문에 있는가)를 늘 통과한다."""
    text = plain(text)
    found: list[list[dict]] = []
    for f in rules["fields"]:
        hits: list[dict] = []
        seen: set[str] = set()
        for m in re.finditer(f["pattern"], text, re.M):
            name = re.sub(r"\s+", " ", (m.group(1) if m.re.groups else m.group(0)) or "").strip()[:200]
            if name and name not in seen:
                seen.add(name)
                hits.append({"cls": f["class"], "name": name, "evidence": _line_at(text, m.start(), m.end(), name)})
            if len(hits) >= MATCHES_PER_FIELD:
                break
        found.append(hits)
    nodes = [n for hits in found for n in hits]
    edges = []
    for r in rules.get("relations") or []:
        for a in found[r["from"]][:1]:          # 주인공은 하나 — 첫 것
            for b in found[r["to"]]:
                edges.append({"src_cls": a["cls"], "src": a["name"], "rel": r["rel"], "dst_cls": b["cls"],
                              "dst": b["name"], "evidence": b["evidence"]})
    missing = [i for i, f in enumerate(rules["fields"]) if f.get("required") and not found[i]]
    return nodes, edges, missing


# --- 견주기 ---

def _norm(name: str) -> str:
    return sm.normalize(name, sm.DEFAULT_RULES)


def _keys(nodes: list[dict], edges: list[dict]) -> set[tuple]:
    return ({("n", n["cls"], _norm(n["name"])) for n in nodes}
            | {("e", e["rel"], _norm(e["src"]), _norm(e["dst"])) for e in edges})


def compare(found: tuple[list[dict], list[dict]], reference: tuple[list[dict], list[dict]],
            classes: set[str] | None = None) -> dict:
    """규칙이 뽑은 것 vs LLM이 뽑은 것(정답으로 본다). 재현율은 규칙이 다루는 개념(classes)의 정답만 센다 —
    규칙에 없는 개념까지 세면 재현율은 늘 낮고, 그것은 규칙이 틀린 것이 아니라 그 개념을 맡지 않은 것이다."""
    got, want = _keys(*found), _keys(*reference)
    if classes is not None:
        want = {k for k in want if (k[0] == "n" and k[1] in classes) or k[0] == "e"}
    hit = got & want
    return {"found": len(got), "expected": len(want), "hit": len(hit),
            "extra": sorted(" ".join(k[1:]) for k in got - want)[:10],
            "missed": sorted(" ".join(k[1:]) for k in want - got)[:10]}


def evaluate(rules: dict, docs: list[dict]) -> dict:
    """docs = [{path, text, nodes, edges}] — 문서마다 견주고 모은다. 정밀도 = 맞은 것 / 규칙이 뽑은 것."""
    classes = {f["class"] for f in rules["fields"]}
    per_doc, found, hit, expected = [], 0, 0, 0
    for d in docs:
        nodes, edges, missing = apply_rules(rules, d["text"])
        c = compare((nodes, edges), (d["nodes"], d["edges"]), classes)
        per_doc.append({"path": d["path"], **c, "missing_required": missing})
        found, hit, expected = found + c["found"], hit + c["hit"], expected + c["expected"]
    return {"precision": hit / found if found else 0.0, "recall": hit / expected if expected else 0.0,
            "docs": per_doc, "escalated": sum(1 for d in per_doc if d["missing_required"])}


def rules_version(rules: dict) -> str:
    return hashlib.sha1(json.dumps(rules, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
