"""경로 틀 — 저장소의 폴더·파일 이름에서 뜻을 가진 칸을 찾는다. **판정만 한다**(파일·그래프를 바꾸지 않는다).

사내 문서 폴더의 뜻은 계층보다 **이름 안의 식별자**에 있다(운영 서버 실측, 2026-11):

  건 폴더     contracts — 폴더 1,079개 중 1,078개가 `KR-CT{YYMM}{순번}_{차수}`이고 안의 파일 7,707건 모두 그 번호로
              시작한다. 폴더 하나가 계약 하나다
  대상 폴더   credit — 241개 모두 `KR{6자리}_{회사명}`, 안에는 「22년 신용평가.pdf」. 폴더 하나가 협력사 하나다
  파일 이름   intro — 폴더가 없고 161건 중 147건의 파일 이름에 `_KR{6자리}_`(협력사 코드). credit의 코드와 120개가 같다
  종류/기간   work-square — `WPR(팀)/2024년/…`, `KPI/2024/경쟁평가율/Raw/…`, `…/History/…`

그래서 칸의 역할을 이렇게 가른다:

  identifier  형제 폴더 중 앞머리 모양(숫자를 자릿수로 바꾼 틀)이 같은 것이 5개 이상이고, 영문자와 숫자 4자리 이상이
              섞인 것 — 폴더 하나가 개체 하나. 뒤따르는 부분은 number(「_1」 — 차수)나 name(「_MEKAS CO.,LTD」)
  year        이름 전체가 연도·연월인 폴더(2024, 2024년, 24년, 2026-10)
  version     옛 판을 모은 폴더(History, old, 구버전, 백업, archive)
  raw         원자료 폴더(Raw, 원본, 원자료)
  category    식별자도 연도도 아닌 형제 폴더 — 문서 종류일 가능성이 높다(WPR(팀)·KPI·MBR(팀))
  파일 칸     같은 묶음(식별자·연도 폴더는 형제를 한 묶음으로)의 파일 이름에 60% 이상 나오는 식별자 토큰·연도 토큰

판정의 근거: 분류 트리의 노드 뜻을 경로 라벨들의 결합으로 보는 방식(Giunchiglia 외, *Encoding classifications into
lightweight ontologies*, 2007)과 계층 라벨을 경로 문맥 안에서 해석하는 방식(Bouquet 외, CtxMatch, ISWC 2003). 칸과
개념의 대응(이 식별자는 「계약」)은 사람이 확정한다 — 여기서는 후보만 낸다.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from . import docsearch
from . import storage as storage_service

MIN_SIBLINGS = 5        # 형제 폴더가 이만큼 있어야 이름 모양으로 식별자를 판정한다
ID_SHARE = 0.9          # 형제 중 같은 모양의 비율
FILE_SHARE = 0.6        # 묶음의 파일 중 그 토큰이 나오는 비율
MIN_FILES = 5           # 파일 칸을 볼 묶음의 최소 파일 수
EXAMPLES = 3

_YEAR_NAME = re.compile(r"^(?:(?:19|20)\d{2}(?:[-_.](?:0[1-9]|1[0-2]))?|(?:19|20)?\d{2}\s*년(?:도)?)$")
_YEAR_TOKEN = re.compile(r"(?<![\d~\-])(?:(?:19|20)\d{2}|\d{2})\s*년")
_VERSION = re.compile(r"^(history|old|_old|구버전|이전\s*버전|이전|백업|backup|archive|보관)$", re.I)
_RAW = re.compile(r"^(raw|raw\s*data|원본|원자료|원천\s*자료)$", re.I)
_LEAD = re.compile(r"^([^_\s]+)([_\s].*)?$")
_TOKEN_SPLIT = re.compile(r"[_\s\[\]()]+")


def _piece(text: str) -> str:
    """숫자 묶음을 자릿수로, 나머지 글자는 그대로(이스케이프)."""
    out, last = [], 0
    for m in re.finditer(r"\d+", text):
        out.append(re.escape(text[last:m.start()]))
        out.append(rf"\d{{{len(m.group())}}}")
        last = m.end()
    out.append(re.escape(text[last:]))
    return "".join(out)


def shape(text: str) -> str:
    """이름 조각의 모양 — 숫자 묶음을 자릿수로(KR-CT230700629 → KR-CT\\d{9}). 「-」로 나눈 조각 중 숫자 4자리 묶음이 없는
    짧은 영숫자 혼합(doc-20261028-**gh6n**의 끝처럼 무작위로 붙인 것)은 [a-z0-9]{n}으로 — 그대로 두면 형제마다 모양이 갈린다."""
    out = []
    for part in text.split("-"):
        if (re.fullmatch(r"[A-Za-z0-9]{2,8}", part) and re.search(r"\d", part) and re.search(r"[A-Za-z]", part)
                and not re.search(r"\d{4}", part)):
            out.append(f"[a-z0-9]{{{len(part)}}}")
        else:
            out.append(_piece(part))
    return r"\-".join(out)


def _is_identifier(token: str) -> bool:
    """식별자 모양 — 영문자가 있고 숫자가 4자리 이상. 숫자만인 것(20251210 · 251217)은 날짜일 수 있고, 한글과 숫자
    (「3080억원기준」)는 금액·수량이라 뺀다."""
    return bool(re.search(r"[A-Za-z]", token)) and sum(c.isdigit() for c in token) >= 4


def _folder_role(name: str) -> str:
    n = name.strip()
    if _YEAR_NAME.match(n):
        return "year"
    if _VERSION.match(n):
        return "version"
    if _RAW.match(n):
        return "raw"
    return ""


def analyze(paths: list[str]) -> dict:
    """경로 목록 → 칸 판정. 저장소에 묶이지 않은 순수 함수(시험·운영 도구가 같이 쓴다)."""
    children: dict[str, set[str]] = defaultdict(set)   # 부모 폴더 → 자식 폴더 이름
    files: dict[str, list[str]] = defaultdict(list)     # 폴더 → 파일 이름
    docs_under: Counter = Counter()                     # 폴더 → 그 아래 파일 수
    for p in paths:
        parts = p.split("/")
        folder = "/".join(parts[:-1])
        files[folder].append(parts[-1])
        for i in range(len(parts) - 1):
            children["/".join(parts[:i])].add(parts[i])
            docs_under["/".join(parts[:i + 1])] += 1

    def join(parent: str, name: str) -> str:
        return f"{parent}/{name}" if parent else name

    slots, marked = [], []
    group_of: dict[str, str] = {}     # 폴더 → 파일 칸을 볼 묶음(식별자·연도 폴더는 「부모/*」)
    for parent, kids in sorted(children.items()):
        roles = {k: _folder_role(k) for k in kids}
        for k, r in roles.items():
            if r:
                marked.append({"folder": join(parent, k), "role": r, "documents": docs_under[join(parent, k)]})
                if r == "year":
                    group_of[join(parent, k)] = join(parent, "*")
        rest = [k for k in kids if not roles[k]]
        if len(rest) < (2 if not parent else MIN_SIBLINGS):
            continue
        if len(rest) < MIN_SIBLINGS:        # 맨 위의 종류(WPR(팀)·KPI·MBR(팀)) — 식별자를 판정하기에는 적다
            slots.append({"where": "folder", "parent": parent, "role": "category", "siblings": len(kids),
                          "names": sorted(rest, key=lambda k: -docs_under[join(parent, k)])[:12],
                          "documents": sum(docs_under[join(parent, k)] for k in rest)})
            continue
        leads = {k: _LEAD.match(k) for k in rest}
        members_of: dict[str, list[str]] = defaultdict(list)
        for k, m in leads.items():
            if m:
                members_of[shape(m.group(1))].append(k)
        # 같은 모양이 MIN_SIBLINGS 이상이고 식별자 모양이면 식별자 칸 — 한 부모 아래 두 갈래(계약 폴더와 협력사 폴더)가
        # 섞여 있어도 갈래마다 따로 낸다. 모양이 갈래마다 다르므로 섞여도 서로를 가리지 않는다.
        families = [(pat, sorted(ks)) for pat, ks in sorted(members_of.items(), key=lambda kv: -len(kv[1]))
                    if len(ks) >= MIN_SIBLINGS and _is_identifier(leads[ks[0]].group(1))]
        covered = {k for _, ks in families for k in ks}
        for top, members in families:
            tails = [(leads[k].group(2) or "").lstrip("_ ").strip() for k in members]
            numeric = [t for t in tails if t.isdigit()]
            tail = ("number" if tails and len(numeric) / len(tails) >= ID_SHARE else
                    "name" if tails and sum(bool(t) for t in tails) / len(tails) >= ID_SHARE else "")
            slot = {"where": "folder", "parent": parent, "role": "identifier", "pattern": top,
                    "share": round(len(members) / len(rest), 3), "count": len(members), "siblings": len(kids),
                    "documents": sum(docs_under[join(parent, k)] for k in members),
                    "examples": members[:EXAMPLES], "values": len({leads[k].group(1) for k in members}),
                    "outside": sorted(set(rest) - covered)[:EXAMPLES]}   # 틀 밖(수집이 쓴 폴더 등)
            if tail:
                slot["tail"] = tail
                if tail == "number":
                    slot["tail_values"] = dict(sorted(Counter(int(t) for t in numeric).items())[:12])
            slots.append(slot)
            for k in members:
                group_of[join(parent, k)] = join(parent, "*")
        if not families:
            slots.append({"where": "folder", "parent": parent, "role": "category", "siblings": len(kids),
                          "names": sorted(rest, key=lambda k: -docs_under[join(parent, k)])[:12],
                          "documents": sum(docs_under[join(parent, k)] for k in rest)})

    # 파일 칸 — 묶음마다 파일 이름의 식별자·연도 토큰
    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)   # 묶음 → [(폴더, 파일 이름)]
    for folder, names in files.items():
        g = group_of.get(folder, folder)
        groups[g] += [(folder, n) for n in names]
    merged: dict[tuple[str, str], dict] = {}
    for g, items in sorted(groups.items()):
        if len(items) < MIN_FILES:
            continue
        seen: Counter = Counter()
        same_as_folder: Counter = Counter()
        example: dict[str, str] = {}
        for folder, name in items:
            stem = name.rsplit(".", 1)[0]
            lead = folder.rsplit("/", 1)[-1].split("_")[0] if folder else ""
            found = set()
            for tok in _TOKEN_SPLIT.split(stem):
                if _is_identifier(tok):
                    found.add(("identifier", shape(tok)))
                    same_as_folder[shape(tok)] += tok == lead
                    example.setdefault(shape(tok), tok)
            for y in _YEAR_TOKEN.finditer(stem):
                found.add(("year", shape(y.group().replace(" ", ""))))
                example.setdefault(shape(y.group().replace(" ", "")), y.group())
            for f in found:
                seen[f] += 1
        for (role, pat), n in seen.most_common():
            if n / len(items) < FILE_SHARE:
                break
            # 같은 모양은 하나로 — 묶음(폴더)마다 따로 내면 하위 폴더마다 같은 「YY년」이 되풀이된다
            m = merged.setdefault((role, pat), {"where": "file", "role": role, "pattern": pat, "count": 0, "files": 0,
                                                "groups": [], "example": example.get(pat, ""), "folder_hits": 0})
            m["count"] += n
            m["files"] += len(items)
            m["groups"].append(g)
            m["folder_hits"] += same_as_folder[pat]
    for m in merged.values():
        m["share"] = round(m["count"] / m["files"], 3)
        if m["role"] == "identifier" and m["folder_hits"] >= m["count"] * ID_SHARE:
            m["repeats_folder"] = True    # 파일 이름이 폴더의 식별자를 되풀이한다(contracts)
        del m["folder_hits"]
        m["groups"] = m["groups"][:EXAMPLES * 2]
        slots.append(m)

    folders = len(docs_under)
    top_id = max((s for s in slots if s["where"] == "folder" and s["role"] == "identifier" and not s["parent"]),
                 key=lambda s: s["documents"], default=None)
    in_years = sum(1 for p in paths if any(_folder_role(x) == "year" for x in p.split("/")[:-1]))
    kind = ("평평함" if not folders else
            "대상 폴더" if top_id and top_id.get("tail") == "name" and top_id["documents"] >= 0.5 * len(paths) else
            "건 폴더" if top_id and top_id["documents"] >= 0.5 * len(paths) else
            "종류/기간" if in_years >= 0.5 * len(paths) else "계층")
    return {"documents": len(paths), "folders": folders,
            "depth": dict(sorted(Counter(p.count("/") for p in paths).items())),
            "kind": kind, "slots": slots, "marked": marked[:60]}


def _paths(store_name: str) -> list[str]:
    if not docsearch.index_path(store_name).exists():
        return []
    conn = docsearch._connect(store_name)
    try:
        return [r["path"] for r in conn.execute("SELECT path FROM docs ORDER BY path")]
    finally:
        conn.close()


def profile(store_name: str) -> dict:
    """저장소 하나의 경로 틀 — 색인의 경로(docs)로 본다(주기 색인만큼 늦다). 색인이 없으면 문서 0건."""
    return {"store": store_name, **analyze(_paths(store_name))}


def match(path: str, slot: dict) -> list[tuple[str, str, str]]:
    """경로 하나에서 칸의 값 — [(값, 뒤따르는 부분, 그 칸이 있던 이름)]. 폴더 칸은 많아야 하나(그 깊이의 폴더 이름),
    파일 칸은 파일 이름의 토큰마다. 폴더 칸은 그 폴더 **아래의 파일**에만 걸린다(폴더 이름 = 파일이 속한 개체)."""
    names = path.split("/")
    rx = re.compile(slot["pattern"])
    if slot["where"] == "folder":
        parent = slot.get("parent") or ""
        depth = parent.count("/") + 1 if parent else 0
        if len(names) <= depth + 1 or (parent and not path.startswith(parent + "/")):
            return []
        m = _LEAD.match(names[depth])
        if m and rx.fullmatch(m.group(1)):
            return [(m.group(1), (m.group(2) or "").lstrip("_ ").strip(), names[depth])]
        return []
    return [(t, "", names[-1]) for t in _TOKEN_SPLIT.split(names[-1].rsplit(".", 1)[0]) if rx.fullmatch(t)]


def _values(store_name: str, slot: dict) -> set[str]:
    """식별자 칸의 값 — 저장소 사이를 잇는 데 쓴다."""
    return {v for p in _paths(store_name) for v, _, _ in match(p, slot)}


def profile_all() -> dict:
    """색인 대상 저장소 전부 — 저장소마다 경로 틀과, **같은 모양의 식별자가 여러 저장소에 나오면** 값이 겹치는 수(저장소
    사이의 연결 후보: credit 폴더의 KR{6자리}와 intro 파일 이름의 KR{6자리})."""
    stores = [profile(s.name) for s in storage_service.indexed_stores()]
    by_pattern: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for st in stores:
        for slot in st["slots"]:
            if slot["role"] == "identifier" and not slot.get("repeats_folder"):
                by_pattern[slot["pattern"]].append((st["store"], slot))
    links = []
    for pat, found in sorted(by_pattern.items()):
        names = sorted({s for s, _ in found})
        if len(names) < 2:
            continue
        values = {s: set() for s in names}
        for s, slot in found:
            values[s] |= _values(s, slot)
        common = set.intersection(*values.values())
        links.append({"pattern": pat, "stores": {s: len(v) for s, v in values.items()}, "shared": len(common),
                      "examples": sorted(common)[:EXAMPLES]})
    return {"stores": stores, "links": links}
