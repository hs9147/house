"""데이터 수집의 정보 현황 — 지금 무엇을 관리하고 있고(인벤토리), 사이트에서 무엇을 더 받을 수 있으며(발견), 그것이
**신규**인지(판정) 가려 수집 조건에 더한다(추가).

  인벤토리   출처마다 관리하는 것 — 수집 조건의 화면(목록·페이지·파일), 스킬, 누적 목록(건수·열·마지막 수집),
             저장한 파일(유형·저장소), API 엔드포인트·MCP 도구. 유형별 합계를 함께.
  발견       출처의 지난 스캔이 읽은 것 — 조회 화면(조건 칸이 있는 화면)·일반 페이지·내려받은 파일·API·MCP 도구.
             로그인 화면은 뺀다.
  판정       항목마다 managed(이 출처에서 이미 수집) · duplicate(다른 출처에서 같은 것을 관리 — 같은 주소·같은 결과 열·
             같은 파일 내용) · similar(이름이 비슷한 것을 관리) · new(신규). 결정론 규칙이 먼저고, LLM은 비슷함·신규만
             다시 본다(뜻이 같은데 이름만 다른 것 — 「구매 계약 현황」과 「계약 목록」). LLM이 없으면 규칙만.
  제안       **사람은 입력하지 않는다.** 버튼 하나로 LLM이 판정·추가할 화면·받을 것·조회 조건(infosource.suggest_queries)·
             스킬 초안(skills.draft)까지 만든 제안을 뒤에서 만든다(start_proposal). 사람은 제안을 보고 **확정만** 한다
             (confirm) — 기본으로 체크된 것이 신규이고, 빼고 싶은 것만 끈다.
  추가       확정한 화면을 수집 조건에 더한다 — 조회 화면은 「파일」(목록·첨부), 일반 페이지는 「페이지」(본문)로. 저장은
             수집 조건과 같은 길(infosource.clean_screens)이다.

판정의 근거(공개 연구):
  - **Table Union Search**(Nargesian 외, VLDB 2018) — 새 표가 이미 있는 표와 "합칠 수 있는가"(같은 정보인가)를 열의
    겹침으로 본다. 여기서는 결과 열 이름(정규화)의 Jaccard: UNION 이상이면 같은 정보(duplicate), OVERLAP 이상이면 비슷함.
  - **스키마 매칭 개관**(Rahm & Bernstein, VLDB J. 2001) — 이름 기반(문자열 유사도)과 구조 기반(열 집합)을 함께 본다:
    화면 이름 유사도(Ratcliff/Obershelp, difflib)와 열 겹침 중 큰 쪽.
  - **LLM으로 대상 판정**(Peeters & Bizer, 2023 — Entity Matching using LLMs) — 문턱 사이의 애매한 것만 여러 개를 한 번에
    묻는다(semantic_merge와 같은 구조). 규칙이 확신한 것(managed·duplicate)은 LLM에 다시 묻지 않는다.
"""
from __future__ import annotations

import difflib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import InfoSource
from . import infosource, jobs, llm
from .infosource import SourceError

STATUSES = ("managed", "duplicate", "similar", "new")
SIMILAR = 0.8                 # 이름 유사도(정규화 뒤) — 이 이상이면 비슷함
UNION = 0.8                   # 결과 열 Jaccard — 이 이상이면 같은 정보(합칠 수 있는 표, Table Union Search)
OVERLAP = 0.4                 # 이 이상이면 비슷함(LLM이 다시 본다)
MAX_ITEMS = 300
_CATEGORY_LABEL = {k: v[0] for k, v in infosource.CATEGORIES.items()}


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or "").lower())


def _base(url: str) -> str:
    return str(url or "").split("#")[0]


# ---------------------------------------------------------------- 인벤토리

def _dataset(row: InfoSource, url: str) -> dict | None:
    path = infosource._dataset_dir(row.id) / f"{infosource._tag(url)}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None
    if not data:
        return None
    return {"rows": data.get("rows") or len(data.get("records") or []), "last": data.get("last") or "",
            "updated": data.get("updated") or "",
            "columns": [c.get("label") or c["key"] for c in data.get("columns") or [] if c.get("key")]}


def _managed(row: InfoSource) -> dict:
    """출처 하나가 관리하는 것."""
    screens = []
    for s in infosource.query_screens(row):
        ds = _dataset(row, s["url"])
        screens.append({"screen": s["screen"], "url": s["url"], "collect": s.get("collect") or "files",
                        "type": "목록" if ds else ("페이지" if s.get("collect") == "page" else "조회 화면"),
                        "skill": (s.get("skill") or {}).get("name") or "", "conditions": len(s["conditions"]),
                        "dataset": ds})
    files: dict[str, dict] = {}
    for f in row.saved_files or []:
        b = files.setdefault(f["category"], {"category": f["category"], "label": _CATEGORY_LABEL.get(f["category"], ""),
                                             "count": 0, "stores": []})
        b["count"] += 1
        if f["store"] not in b["stores"]:
            b["stores"].append(f["store"])
    scan = row.scan or {}
    return {"id": row.id, "name": row.name, "kind": row.kind, "url": row.url, "status": row.status,
            "scanned_at": row.scanned_at, "saved_at": row.saved_at, "screens": screens, "files": list(files.values()),
            "endpoints": len(scan.get("endpoints") or []) if row.kind == "api" else 0,
            "tools": len(scan.get("tools") or []) if row.kind == "mcp" else 0}


def inventory(db: Session) -> dict:
    """모든 출처가 관리하는 데이터의 종류와 유형 — 출처별 목록과 유형별 합계."""
    rows = list(db.execute(select(InfoSource).order_by(InfoSource.id)).scalars())
    sources = [_managed(r) for r in rows]
    totals = {"목록": 0, "목록 건수": 0, "조회 화면": 0, "페이지": 0, "스킬": 0, "API 엔드포인트": 0, "MCP 도구": 0}
    by_file: dict[str, int] = {}
    for s in sources:
        for sc in s["screens"]:
            totals[sc["type"]] += 1
            totals["스킬"] += bool(sc["skill"])
            if sc["dataset"]:
                totals["목록 건수"] += sc["dataset"]["rows"]
        for f in s["files"]:
            by_file[f["label"] or f["category"]] = by_file.get(f["label"] or f["category"], 0) + f["count"]
        totals["API 엔드포인트"] += s["endpoints"]
        totals["MCP 도구"] += s["tools"]
    return {"sources": sources, "totals": totals, "files": by_file,
            "kinds": {k: sum(1 for s in sources if s["kind"] == k) for k in infosource.KINDS}}


# ---------------------------------------------------------------- 발견 + 판정

def _catalog(db: Session) -> dict:
    """판정의 기준 — 모든 출처가 관리하는 화면(주소·이름·결과 열)과 저장한 파일 내용."""
    screens, shas = [], {}
    for r in db.execute(select(InfoSource)).scalars():
        for s in infosource.query_screens(r):
            ds = _dataset(r, s["url"])
            screens.append({"source_id": r.id, "source": r.name, "screen": s["screen"], "url": s["url"],
                            "skill": (s.get("skill") or {}).get("name") or "",
                            "columns": frozenset(_norm(c) for c in (ds or {}).get("columns") or [])})
        for f in r.saved_files or []:
            if f.get("sha"):
                shas[f["sha"]] = {"source_id": r.id, "source": r.name, "path": f["path"]}
    return {"screens": screens, "shas": shas}


def _judge(row: InfoSource, item: dict, cat: dict) -> tuple[str, str]:
    """(상태, 까닭) — 결정론 규칙."""
    url, title = _base(item.get("url") or ""), item.get("title") or ""
    cols = frozenset(_norm(c) for c in item.get("columns") or [])
    for s in cat["screens"]:
        same_url = _base(s["url"]) == url and url
        if same_url and s["source_id"] == row.id:
            return "managed", f"수집 조건 「{s['screen']}」으로 받고 있습니다"
        if same_url:
            return "duplicate", f"다른 출처 「{s['source']}」의 「{s['screen']}」과 같은 주소입니다"
    best, score, why = None, 0.0, ""
    for s in cat["screens"]:
        if len(cols) >= 2 and len(s["columns"]) >= 2:
            j = len(cols & s["columns"]) / len(cols | s["columns"])
            if j >= UNION:
                where = "이 출처" if s["source_id"] == row.id else f"「{s['source']}」"
                return "duplicate", f"{where}의 「{s['screen']}」 목록과 결과 열이 {j:.0%} 겹칩니다(같은 정보)"
            if j >= OVERLAP and j > score:
                best, score, why = s, j, f"결과 열이 {j:.0%} 겹칩니다"
        for name in (s["screen"], s["skill"]):
            r = difflib.SequenceMatcher(None, _norm(title), _norm(name)).ratio() if title and name else 0
            if r >= SIMILAR and r > score:
                best, score, why = s, r, f"이름이 비슷합니다({r:.0%})"
    if best:
        return "similar", f"「{best['source']}」의 「{best['screen']}」과 {why}"
    return "new", ""


def discover(db: Session, row: InfoSource) -> dict:
    """이 출처의 지난 스캔이 읽은 것마다 신규인지 — 조회 화면·페이지·파일·API·MCP 도구."""
    scan = row.scan or {}
    if not scan:
        raise SourceError("스캔한 적이 없습니다 — 먼저 스캔하세요.")
    cat = _catalog(db)
    items = []
    if row.kind == "web":
        for p in scan.get("pages") or []:
            if p.get("requires_login") or p.get("kind") == "login":
                continue
            item = {"type": "조회 화면" if p.get("fields") else "페이지", "url": p["url"],
                    "title": p.get("title") or p["url"], "info": (p.get("info") or "")[:200],
                    "fields": (p.get("fields") or [])[:15], "columns": ((p.get("tables") or [[]])[0] or [])[:20],
                    "has_path": bool(p.get("path")), "addable": True}
            items.append(item)
        for f in scan.get("files") or []:
            saved = cat["shas"].get(f.get("sha") or "")
            status, why = (("managed" if saved["source_id"] == row.id else "duplicate"),
                           f"「{saved['source']}」에 저장돼 있습니다({saved['path']})") if saved else ("new", "")
            items.append({"type": f"파일 · {_CATEGORY_LABEL.get(f.get('category'), '')}", "url": f["url"],
                          "title": f["name"], "info": f.get("text") or "", "addable": False,
                          "status": status, "reason": why or "저장 위치에서 저장하면 저장소에 들어갑니다"})
    elif row.kind == "api":
        for e in scan.get("endpoints") or []:
            items.append({"type": "API 엔드포인트", "url": e["path"], "title": f"{e['method']} {e['path']}",
                          "info": e.get("summary") or "", "fields": e.get("params") or [], "addable": False,
                          "status": "managed" if row.saved_at else "new",
                          "reason": "정리 문서로 저장했습니다" if row.saved_at else "저장하면 정리 문서로 들어갑니다"})
    else:
        for t in scan.get("tools") or []:
            items.append({"type": "MCP 도구" + ("" if t.get("read_only") else "(쓰기)"), "url": t["name"],
                          "title": t["name"], "info": t.get("description") or "", "fields": t.get("params") or [],
                          "addable": False, "status": "managed" if row.saved_at else "new",
                          "reason": "정리 문서로 저장했습니다" if row.saved_at else "저장하면 정리 문서로 들어갑니다"})
    decided = decisions(row.id)
    for item in items:
        human = decided.get(item["url"])
        if human:
            item["status"], item["reason"], item["decided"] = human["status"], f"사람이 정함({human['by']})", True
        elif "status" not in item:
            item["status"], item["reason"] = _judge(row, item, cat)
    items = items[:MAX_ITEMS]
    counts = {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}
    return {"source": row.name, "kind": row.kind, "scanned_at": row.scanned_at, "items": items, "counts": counts,
            "screens": len(infosource.query_screens(row)), "max_screens": infosource.MAX_QUERY_SCREENS}


JUDGE_PROMPT = """사내 업무 사이트에서 새로 찾은 화면이 **이미 관리하는 정보와 같은 것인지** 판단한다. 이름이 달라도 같은 목록·
같은 업무 정보면 같다(예: 「구매 계약 현황」과 「계약 목록」, 결과 열이 대부분 겹친다). 화면이 보여 주는 정보의 뜻으로
판단하고, 모르면 new로 둔다. 출력은 JSON 객체 하나다(설명·코드펜스 없이).

{{"items": [{{"i": 번호, "status": "duplicate|new", "same_as": "같다고 본 관리 화면 이름(없으면 빈 값)",
             "reason": "한 문장"}}]}}

이미 관리하는 화면:
{managed}

새로 찾은 화면:
{found}
"""


def judge_with_llm(db: Session, row: InfoSource) -> dict:
    """비슷함·신규 판정을 LLM이 뜻으로 다시 본다 — 규칙으로 managed·duplicate인 것과 사람이 정한 것은 그대로. 저장소의
    유형과 겹친 것(하네스 — store_match)은 비슷함으로 와서 LLM이 본다."""
    latest = _read(_side_path(row.id, "latest"), {})
    out = discover(db, row)
    if latest.get("items"):        # 하네스 라운드의 교차 판정(저장소 유형과의 겹침)을 잇는다
        crossed = {i["url"]: i for i in latest["items"] if i.get("store_match")}
        for item in out["items"]:
            c = crossed.get(item["url"])
            if c and item["status"] == "new" and not item.get("decided"):
                item.update(status=c["status"], reason=c["reason"], store_match=c["store_match"])
    provider = llm.default_provider(db)
    if provider is None:
        out["notes"] = ["기본 LLM이 없어 규칙 판정만 했습니다."]
        return out
    targets = [(k, i) for k, i in enumerate(out["items"])
               if i.get("addable") and i["status"] in ("similar", "new") and not i.get("decided")]
    if not targets:
        return out
    cat = _catalog(db)
    managed = [{"source": s["source"], "screen": s["screen"], "skill": s["skill"],
                "columns": sorted(s["columns"])[:15]} for s in cat["screens"]][:150]
    found = [{"i": n, "title": i["title"], "info": i["info"], "fields": i.get("fields") or [],
              "columns": i.get("columns") or []} for n, (_, i) in enumerate(targets)][:80]
    try:
        data = infosource._parse(llm.chat_completion(provider, [{"role": "user", "content": JUDGE_PROMPT.format(
            managed=json.dumps(managed, ensure_ascii=False), found=json.dumps(found, ensure_ascii=False))}], db))
    except Exception as e:  # noqa: BLE001 — 판정은 규칙으로도 된다
        out["notes"] = [f"LLM 판정을 하지 못해 규칙 판정만 했습니다: {str(e)[:120]}"]
        return out
    changed = 0
    for j in data.get("items") or []:
        if not isinstance(j, dict) or not isinstance(j.get("i"), int) or not 0 <= j["i"] < len(found):
            continue
        item = out["items"][targets[j["i"]][0]]
        if j.get("status") == "duplicate":
            same = str(j.get("same_as") or "")[:100]
            item["status"], item["reason"] = "duplicate", f"LLM: 「{same}」과 같은 정보 — {str(j.get('reason') or '')[:150]}"
            changed += 1
        elif j.get("status") == "new" and item["status"] == "similar":
            item["status"], item["reason"] = "new", f"LLM: 이름은 비슷하지만 다른 정보 — {str(j.get('reason') or '')[:150]}"
            changed += 1
    out["counts"] = {s: sum(1 for i in out["items"] if i["status"] == s) for s in STATUSES}
    out["notes"] = [f"LLM이 {len(found)}개를 다시 보고 {changed}개의 판정을 바꿨습니다."]
    out["judged_by"] = "llm"
    return out


# ---------------------------------------------------------------- 제안(사람 입력 없이) → 확정

def _proposal_path(source_id: int):
    from pathlib import Path  # noqa: PLC0415

    from ..config import get_settings  # noqa: PLC0415

    root = Path(get_settings().storage_root or "./data/storage").resolve()
    return root / ".infosource" / "coverage" / f"{source_id}.json"


def proposal(source_id: int) -> dict | None:
    path = _proposal_path(source_id)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None


def _save_proposal(source_id: int, data: dict) -> None:
    path = _proposal_path(source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")


def build_proposal(db: Session, row: InfoSource, step=lambda _t: None) -> dict:
    """사람 입력 없이 만드는 제안 — ① 판정(규칙 → 애매한 것만 LLM) ② 신규 화면 중 수집 조건에 넣을 자리만큼(조회 화면
    먼저) ③ 조회 화면은 LLM이 수집 방법·조건을 정하고(infosource.suggest_queries — 사실에 있는 칸만) ④ 스킬 초안을
    붙인다. 저장하지 않는다 — 확정(confirm)에서 더한다."""
    from datetime import datetime, timezone  # noqa: PLC0415

    from . import skills  # noqa: PLC0415

    step("저장소의 데이터 유형과 견주는 중(하네스 라운드)")
    run_round(db, row, "정보 현황 분석")
    step("수집할 수 있는 정보를 판정하는 중(규칙 → 애매한 것만 LLM)")
    found = judge_with_llm(db, row)
    room = max(0, infosource.MAX_QUERY_SCREENS - found["screens"])
    new = [i for i in found["items"] if i.get("addable") and i["status"] == "new"]
    new.sort(key=lambda i: (i["type"] != "조회 화면", i["title"]))
    chosen, over = new[:room], new[room:]
    planned: dict[str, dict] = {}
    if any(i["type"] == "조회 화면" for i in chosen):
        step("LLM이 신규 조회 화면의 수집 방법·조건을 정하는 중")
        for s in infosource.suggest_queries(db, row)["screens"]:
            planned[s["url"]] = s
    pages = {p["url"]: p for p in (row.scan or {}).get("pages") or []}
    proposed = []
    for k, item in enumerate(chosen):
        step(f"스킬 초안 {k + 1}/{len(chosen)} · {item['title']}")
        p = pages[item["url"]]
        screen = planned.get(item["url"]) or {
            "screen": p.get("title") or item["url"], "url": item["url"], "conditions": [],
            **{x: p[x] for x in ("path", "start", "frame") if p.get(x)}}
        screen = {x: v for x, v in screen.items() if x != "why"}
        if item["type"] == "페이지":
            screen.update(collect="page", page_format="md")
        skill = None
        if item["type"] == "조회 화면":
            try:
                d = skills.draft(db, row, item["url"], screen)
                screen["conditions"], skill = d["conditions"], {**d["skill"], "audience": []}
            except SourceError:
                pass
        proposed.append({"url": item["url"], "title": item["title"], "type": item["type"], "reason": item["reason"],
                         "screen": {**screen, **({"skill": skill} if skill else {})}})
    # 스킬 이름이 겹치면 뒤에 번호 — 확정할 때 같은 이름이 두 화면에 있으면 거부된다
    used = {(s.get("skill") or {}).get("name") for s in infosource.query_screens(row)}
    for p in proposed:
        sk = p["screen"].get("skill")
        if sk:
            base, n = sk["name"], 2
            while sk["name"] in used:
                sk["name"], n = f"{base}-{n}"[:40], n + 1
            used.add(sk["name"])
    notes = list(found.get("notes") or [])
    if over:
        notes.append(f"수집 조건 자리({infosource.MAX_QUERY_SCREENS}개)가 모자라 신규 {len(over)}개는 넣지 않았습니다: "
                     + ", ".join(i["title"] for i in over[:10]))
    return {"source_id": row.id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "counts": found["counts"], "items": found["items"], "proposed": proposed, "notes": notes,
            "judged_by": found.get("judged_by", "rule")}


def start_proposal(db: Session, row: InfoSource) -> None:
    if not row.scan:
        raise SourceError("스캔한 적이 없습니다 — 먼저 스캔하세요.")
    if (proposal(row.id) or {}).get("running"):
        raise SourceError("정보 현황 분석이 이미 돌고 있습니다.")
    infosource.progress_start(row.id, "정보 현황 분석 — 판정·수집 방법·스킬 초안을 만듭니다")
    _save_proposal(row.id, {"source_id": row.id, "running": True})
    _submit(row.id)


def _submit(source_id: int) -> None:
    """테스트에서 인라인으로 바꾸는 큐 경계."""
    jobs.submit(_proposal_job, source_id)


def _proposal_job(source_id: int) -> None:
    from ..db import SessionLocal  # noqa: PLC0415

    with SessionLocal() as session:
        row = session.get(InfoSource, source_id)
        if row is None:
            return
        try:
            _save_proposal(source_id, build_proposal(session, row, lambda t: infosource.progress_step(source_id, t)))
        except Exception as e:  # noqa: BLE001 — 실패 이유는 화면에 그대로 보인다
            _save_proposal(source_id, {"source_id": source_id, "error": str(e)[:500]})
        finally:
            infosource.progress_end(source_id)


def confirm(db: Session, row: InfoSource, urls: list[str], actor: str) -> dict:
    """제안을 확정한다 — 고른 화면을 제안한 그대로(받을 것·조건·스킬) 수집 조건에 더한다. 이미 있는 화면은 건너뛴다.
    확정하면 제안을 지운다(다시 분석하면 새로 만든다)."""
    data = proposal(row.id)
    if not data or not data.get("proposed"):
        raise SourceError("확정할 제안이 없습니다 — 먼저 정보 현황 분석을 하세요.")
    want = set(urls or [])
    screens = infosource.query_screens(row)
    have = {s["url"] for s in screens}
    added = [p["screen"] for p in data["proposed"] if p["url"] in want and p["url"] not in have]
    if added:
        infosource.set_queries(db, row, infosource.clean_screens(row, screens + added), actor)
    _proposal_path(row.id).unlink(missing_ok=True)
    return {"added": [s["url"] for s in added], "skills": [s["skill"]["name"] for s in added if s.get("skill")]}


# ---------------------------------------------------------------- 하네스 — 출처(발견)와 저장소(인벤토리)를 서로 교정하며 되풀이
#
# 출처에서 무엇을 받을 수 있는지와 저장소가 무엇을 갖고 있는지는 **한 번에 맞지 않는다.** 스캔이 화면을 놓치고, 수집이
# 다른 화면을 받고, 저장소에는 출처를 모르는 표가 쌓인다. 그래서 둘을 서로의 기준으로 삼아 라운드마다 다시 잰다:
#
#   출처 → 저장소   새로 찾은 화면의 결과 열이 저장소의 표(문서에 되풀이되는 표·수집 목록)와 겹치면 「비슷함」 — 이미
#                   들어와 있을 수 있다. 제안할 때 LLM이 다시 본다
#   저장소 → 출처   저장소에 되풀이되는 표(양식)인데 어느 출처의 화면과도 겹치지 않으면 「출처 미확인」 — 그 데이터가 어느
#                   시스템에서 오는지 찾을 일이다
#   수집 → 판정     수집 조건의 화면이 0건·막힘·결과 열이 화면 표와 다르면 「교정할 것」 — 판정이 아니라 수집 방법이 틀렸다
#   사람 → 규칙     사람이 고친 판정(decide)은 다음 라운드부터 규칙보다 먼저 쓴다(같은 것을 다시 묻지 않는다)
#
# 라운드는 스캔·수집이 끝날 때마다 돈다(LLM 없이 — 싸다). 라운드마다 수치와 지난 라운드에서 바뀐 판정을 남긴다 —
# 바뀐 것이 없으면 수렴한 것이다. 평가 하네스의 모양(같은 입력을 되풀이해 재고, 차이를 기록한다)을 따른다.

MAX_ROUNDS = 30
STORE_TYPE_MIN_DOCS = 2       # 저장소의 표는 이만큼의 문서에 되풀이될 때 유형으로 본다(한 번 나온 표는 문서의 일부)
GAP_MIN_DOCS = 5              # 이만큼 되풀이되는데 출처가 없으면 「출처 미확인」(양식 — semantic.FORM_MIN_DOCS와 같다)


def _side_path(source_id: int, kind: str):
    return _proposal_path(source_id).with_name(f"{source_id}.{kind}.json")


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default
    except (OSError, ValueError):
        return default


def _write_json(path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")


def decisions(source_id: int) -> dict:
    """사람이 고친 판정 — {url: {status, by, at}}."""
    return _read(_side_path(source_id, "decisions"), {})


def decide(db: Session, row: InfoSource, url: str, status: str, actor: str) -> dict:
    """판정을 고친다 — 다음 라운드·제안부터 규칙·LLM보다 먼저 쓴다. status가 ""이면 고친 것을 지운다(규칙으로 돌아간다)."""
    from datetime import datetime, timezone  # noqa: PLC0415

    if status and status not in STATUSES:
        raise SourceError(f"status는 {', '.join(STATUSES)} 중 하나입니다.")
    data = decisions(row.id)
    if status:
        data[url] = {"status": status, "by": actor, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    else:
        data.pop(url, None)
    _write_json(_side_path(row.id, "decisions"), data)
    return run_round(db, row, f"교정 · {actor}")


def store_types() -> list[dict]:
    """저장소가 관리하는 데이터 유형 — 저장소마다 문서에 되풀이되는 표(머리글이 같은 표)와 데이터 수집 목록(… 목록.csv)."""
    from . import docsearch, semantic  # noqa: PLC0415
    from . import storage as storage_service  # noqa: PLC0415

    out = []
    for store in storage_service.indexed_stores():
        try:
            conn = docsearch._connect(store.name)
        except Exception:  # noqa: BLE001 — 색인이 없는 저장소는 건너뛴다
            continue
        try:
            for r in conn.execute("SELECT name, COUNT(DISTINCT path) AS n FROM nodes WHERE kind = 'table'"
                                  " GROUP BY name HAVING n >= ? ORDER BY n DESC LIMIT 200", (STORE_TYPE_MIN_DOCS,)):
                cols = [c for c in r["name"].split(" | ") if c]
                if len(cols) >= 2:
                    out.append({"store": store.name, "kind": "표", "name": r["name"][:200], "documents": r["n"],
                                "columns": frozenset(_norm(c) for c in cols)})
            for d in semantic._list_docs(conn):
                out.append({"store": store.name, "kind": "수집 목록", "name": d["path"], "documents": 1,
                            "columns": frozenset(_norm(c) for c in d["columns"])})
        finally:
            conn.close()
    return out


def _jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _checks(db: Session, row: InfoSource) -> list[dict]:
    """수집 → 판정: 수집 조건의 화면마다 실제로 받았는지 — 0건·막힘·결과 열이 화면 표와 다름·스킬 실행 0건."""
    from ..models import AuditEvent  # noqa: PLC0415

    pages = {p["url"]: p for p in (row.scan or {}).get("pages") or []}
    collected = {c.get("url"): c for c in (row.scan or {}).get("collected") or []}
    out = []
    for s in infosource.query_screens(row):
        c = collected.get(s["url"])
        if c and c.get("repair"):
            out.append({"url": s["url"], "screen": s["screen"], "kind": "막힘",
                        "detail": (c.get("notes") or ["화면으로 가는 길이 막혔습니다"])[0]})
        elif c and not c.get("rows") and not c.get("data") and not c.get("files") and not c.get("pages"):
            out.append({"url": s["url"], "screen": s["screen"], "kind": "0건",
                        "detail": (c.get("notes") or ["지난 수집이 아무것도 받지 못했습니다"])[0]})
        ds = _dataset(row, s["url"])
        page = pages.get(s["url"]) or pages.get(_base(s["url"])) or {}
        expect = frozenset(_norm(x) for x in ((page.get("tables") or [[]])[0] or []))
        got = frozenset(_norm(x) for x in (ds or {}).get("columns") or [])
        if ds and len(expect) >= 2 and _jaccard(expect, got) < 0.3:
            out.append({"url": s["url"], "screen": s["screen"], "kind": "열 다름",
                        "detail": "받은 목록의 열이 스캔한 화면의 표와 거의 겹치지 않습니다 — 다른 목록을 받고 있을 수 있습니다"})
    for e in db.execute(select(AuditEvent).where(AuditEvent.action == "source.skill.run",
                                                 AuditEvent.target.like(f"{row.name}/%"))
                        .order_by(AuditEvent.id.desc()).limit(20)).scalars():
        d = e.detail or {}
        if d.get("ok") and not d.get("rows") and not d.get("files") and not d.get("pages"):
            out.append({"url": "", "screen": e.target, "kind": "스킬 0건",
                        "detail": (d.get("notes") or ["실행했지만 받은 것이 없습니다"])[0], "at": str(e.created_at)})
            break   # 가장 최근 것 하나 — 같은 것이 쌓이지 않게
    return out


def run_round(db: Session, row: InfoSource, trigger: str = "") -> dict:
    """하네스 한 라운드 — 발견·판정(사람의 교정 먼저) · 저장소 유형과의 교차 · 수집 점검 · 지난 라운드와의 차이."""
    from datetime import datetime, timezone  # noqa: PLC0415

    if not row.scan:
        return {}
    found = discover(db, row)
    types = store_types()
    mine = {(t["store"], t["name"]) for t in types}
    matched: set[tuple] = set()
    for item in found["items"]:
        cols = frozenset(_norm(c) for c in item.get("columns") or [])
        hit = max(((t, _jaccard(cols, t["columns"])) for t in types if len(cols) >= 2), key=lambda x: x[1],
                  default=(None, 0.0))
        if hit[0] and hit[1] >= OVERLAP:
            t = hit[0]
            matched.add((t["store"], t["name"]))
            item["store_match"] = {"store": t["store"], "kind": t["kind"], "name": t["name"][:120],
                                   "documents": t["documents"], "overlap": round(hit[1], 2)}
            if item["status"] == "new" and not item.get("decided"):
                item["status"] = "similar"
                item["reason"] = (f"저장소 「{t['store']}」의 {t['kind']}(문서 {t['documents']}건)와 결과 열이 "
                                  f"{hit[1]:.0%} 겹칩니다 — 이미 들어와 있을 수 있습니다")
    # 다른 출처의 화면이 덮는 저장소 유형도 「출처 있음」이다
    for r in db.execute(select(InfoSource)).scalars():
        for s in infosource.query_screens(r):
            cols = frozenset(_norm(c) for c in (_dataset(r, s["url"]) or {}).get("columns") or [])
            for t in types:
                if _jaccard(cols, t["columns"]) >= OVERLAP:
                    matched.add((t["store"], t["name"]))
    gaps = [{"store": t["store"], "kind": t["kind"], "name": t["name"][:120], "documents": t["documents"]}
            for t in types if (t["store"], t["name"]) in mine - matched and t["documents"] >= GAP_MIN_DOCS][:30]
    issues = _checks(db, row)
    counts = {s: sum(1 for i in found["items"] if i["status"] == s) for s in STATUSES}
    history = _read(_side_path(row.id, "rounds"), [])
    before = (history[-1] if history else {}).get("judged") or {}
    judged = {i["url"]: i["status"] for i in found["items"] if i.get("addable")}
    changed = [{"url": u, "from": before.get(u), "to": st} for u, st in judged.items() if before.get(u) != st]
    rnd = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "trigger": trigger, "counts": counts,
           "items": len(found["items"]), "gaps": len(gaps), "issues": len(issues), "changed": len(changed),
           "decided": len(decisions(row.id)), "judged": judged}
    _write_json(_side_path(row.id, "rounds"), (history + [rnd])[-MAX_ROUNDS:])
    _write_json(_side_path(row.id, "latest"), {"round": rnd, "items": found["items"], "gaps": gaps, "issues": issues,
                                               "changed": changed[:50]})
    return {"round": {k: v for k, v in rnd.items() if k != "judged"}, "gaps": gaps, "issues": issues,
            "changed": changed[:50], "items": found["items"]}


def harness(source_id: int) -> dict:
    """화면·MCP가 읽는 것 — 최신 라운드(판정·출처 미확인·교정할 것·바뀐 판정)와 라운드 이력(수치만)."""
    latest = _read(_side_path(source_id, "latest"), {})
    rounds = [{k: v for k, v in r.items() if k != "judged"} for r in _read(_side_path(source_id, "rounds"), [])]
    if latest.get("round"):
        latest["round"] = {k: v for k, v in latest["round"].items() if k != "judged"}
    return {**latest, "rounds": rounds[-10:], "decisions": decisions(source_id)}


def after_scan(db: Session, row: InfoSource, trigger: str) -> None:
    """스캔·수집이 끝날 때 — 라운드를 돌린다. 실패해도 스캔은 끝난 것이다(삼킨다)."""
    try:
        run_round(db, row, trigger)
    except Exception:  # noqa: BLE001
        pass
