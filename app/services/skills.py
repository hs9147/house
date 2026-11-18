"""수집 스킬 — 데이터 수집의 화면 하나를 **에이전트가 골라 부르는 일**로 만든다.

크롤링(스캔)은 사이트에 무엇이 있는지 읽고, 수집 조건은 화면마다 어떻게 조회하는지 정한다. 스킬은 그 위에
"무엇을 언제 쓰는가"를 붙인다 — Agent Skills가 이름·설명(언제 쓰나)·절차·스크립트로 일을 묶듯이, 화면 하나를

  - 이름과 용도(description·when): 에이전트가 목록에서 보고 고른다
  - 입력(params): 조건 값의 {이름} 자리 — 부를 때 값을 받고, 없으면 기본값
  - 절차: 수집 조건 그대로(메뉴 길·조회 버튼·상세·엑셀) — 결정론으로 다시 밟는다(sourcebot, 북마크와 같은 엔진)
  - 출력(outputs): 결과 목록의 열 — 지난 수집이 받은 열에서 채운다
  - 공개 범위(audience): 부를 수 있는 조직 — 비우면 관리자만

로 묶는다. 스킬은 화면(InfoSourceQuery.screens의 한 항목)의 skill 칸에 둔다 — 화면과 스킬이 따로 놀지 않는다.

**공개 범위가 권한이다.** 서버 봇은 출처에 맡긴 세션으로 사이트에 들어간다. 스킬을 조직에 열면 그 조직 사람(과
그 사람의 에이전트)이 그 세션으로 조회한 결과를 받는다 — 그래서 기본은 관리자만이고, 관리자가 조직을 골라 연다.
실행은 읽기만 한다(엔진이 저장·삭제 버튼을 누르지 않는다). 감사에는 입력의 이름만 남긴다(값에 무엇이 들어 있을지
모른다 — 수집 조건의 값을 LLM에 보내지 않는 것과 같은 판단이다).
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ApiKey, InfoSource
from . import infosource, llm, sourcebot
from .infosource import SourceError

MAX_PARAMS = 12              # 업무 조회 화면은 칸이 많다(실측 GPS4 계약 현황: 코드 칸 6 + 날짜 + 검색어)
MAX_OUTPUTS = 40
MAX_AUDIENCE = 50
RUN_TIMEOUT = 240.0          # 스킬 한 번(화면 하나)의 상한 — 대화가 기다린다
CACHE_SECONDS = 300          # 같은 입력으로 다시 부르면 이 안에서는 지난 결과를 준다(사이트를 다시 두드리지 않게)
MAX_ROWS = 100               # 돌려주는 행(전체 건수는 따로 준다)
MAX_TEXT = 20_000            # 페이지 본문·파일 본문 하나의 상한(글자)
_NAME = re.compile(r"^[0-9A-Za-z가-힣_-]{1,40}$")
PLACEHOLDER = re.compile(r"\{([0-9A-Za-z가-힣_-]{1,40})\}")

_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_CACHE: dict[str, tuple[float, dict]] = {}


def slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", str(text or "")).strip("-")[:40].strip("-")


def _txt(value, cap: int) -> str:
    return str(value or "").strip()[:cap] if isinstance(value, (str, int, float)) else ""


def clean(raw, conditions: list[dict]) -> dict | None:
    """사람이 보낸 스킬 — 이름·입력의 모양을 보고, 조건의 {자리}가 다 입력으로 정의됐는지 본다. 없으면 None."""
    if not isinstance(raw, dict) or not _txt(raw.get("name"), 80):
        return None
    name = slug(raw["name"])
    if not _NAME.match(name):
        raise SourceError(f"스킬 이름은 글자·숫자·_·-로 씁니다: {_txt(raw.get('name'), 80)}")
    params, seen = [], set()
    for p in (raw.get("params") if isinstance(raw.get("params"), list) else [])[:MAX_PARAMS]:
        if not isinstance(p, dict):
            continue
        pname = slug(p.get("name"))
        if not pname:
            continue
        if pname in seen:
            raise SourceError(f"스킬 {name}: 입력 이름이 겹칩니다: {pname}")
        seen.add(pname)
        params.append({"name": pname, "description": _txt(p.get("description"), 200),
                       "default": _txt(p.get("default"), 200), "required": p.get("required") is True})
    used = {m for c in conditions for m in PLACEHOLDER.findall(c.get("value") or "")}
    if used - seen:
        raise SourceError(f"스킬 {name}: 조건에 쓴 자리가 입력에 없습니다: {', '.join(sorted(used - seen))} — 같은 이름의 "
                          "입력을 더하거나, 조건 값에서 {…}를 지우세요(입력 이름과 자리 이름은 같아야 합니다)")
    outputs = [{"name": _txt(o.get("name"), 80), "description": _txt(o.get("description"), 200)}
               for o in (raw.get("outputs") if isinstance(raw.get("outputs"), list) else [])[:MAX_OUTPUTS]
               if isinstance(o, dict) and _txt(o.get("name"), 80)]
    audience = sorted({a for a in (raw.get("audience") if isinstance(raw.get("audience"), list) else [])
                       if isinstance(a, int) and not isinstance(a, bool) and a > 0})[:MAX_AUDIENCE]
    return {"name": name, "description": _txt(raw.get("description"), 500), "when": _txt(raw.get("when"), 300),
            "params": params, "outputs": outputs, "audience": audience}


def check_unique(screens: list[dict]) -> None:
    names = [s["skill"]["name"] for s in screens if s.get("skill")]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise SourceError(f"같은 이름의 스킬이 두 화면에 있습니다: {', '.join(dup)}")


# ---------------------------------------------------------------- 목록·권한

def skill_id(row: InfoSource, name: str) -> str:
    return f"{row.name}/{name}"


def can_use(db: Session, key: ApiKey, skill: dict) -> bool:
    if key.is_admin:
        return True
    from ..security import viewer_org_ids  # noqa: PLC0415

    return bool(set(skill.get("audience") or []) & viewer_org_ids(db, key))


def _columns(row: InfoSource, url: str) -> list[str]:
    """지난 수집이 받은 결과 목록의 열 — 누적본(infosource.merge_dataset)에서. 없으면 빈 목록."""
    path = infosource._dataset_dir(row.id) / f"{infosource._tag(url)}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return []
    return [c.get("label") or c.get("key") for c in data.get("columns") or [] if c.get("key")][:MAX_OUTPUTS]


def runnable() -> tuple[bool, str]:
    """서버가 스킬을 돌릴 수 있는가 — (된다, 안 되는 까닭)."""
    if not infosource.browser_available():
        return False, "서버에 Playwright가 없습니다"
    if not sourcebot.engine_available():
        return False, "수집 엔진 파일(console/src/lib/scanAgent.js)이 서버에 없습니다"
    return True, ""


def _out(row: InfoSource, s: dict) -> dict:
    skill = s["skill"]
    vol = {v["name"]: v for v in infosource.column_volatility(row.id, s["url"])}
    outputs = skill["outputs"] or [{"name": c, "description": ""} for c in _columns(row, s["url"])]
    return {"id": skill_id(row, skill["name"]), "source_id": row.id, "source": row.name, "screen": s["screen"],
            "url": s["url"], **skill,
            # 열마다 고정(지식)·변경(정보) 판정 — 다시 받아 본 건에서 바뀐 비율(infosource.column_volatility)
            "outputs": [{**o, **({"volatility": vol[o["name"]]["hint"], "change_rate": vol[o["name"]]["rate"]}
                                 if o["name"] in vol else {})} for o in outputs],
            "collect": s.get("collect") or "files", "has_path": bool(s.get("path")),
            "session": bool(row.headers_encrypted)}


def listing(db: Session, key: ApiKey | None = None) -> list[dict]:
    """스킬 목록 — key를 주면 그 사람이 부를 수 있는 것만."""
    out = []
    for row in db.execute(select(InfoSource).where(InfoSource.kind == "web").order_by(InfoSource.id)).scalars():
        for s in infosource.query_screens(row):
            if s.get("skill") and (key is None or can_use(db, key, s["skill"])):
                out.append(_out(row, s))
    return out


def find(db: Session, ident: str) -> tuple[InfoSource, dict]:
    """「출처/스킬」 → (출처, 화면). 출처 이름에 /가 있어도 마지막 조각이 스킬이다."""
    source, _, name = str(ident or "").strip().rpartition("/")
    row = db.execute(select(InfoSource).where(InfoSource.name == source)).scalar_one_or_none() if source else None
    for s in infosource.query_screens(row) if row else []:
        if (s.get("skill") or {}).get("name") == name:
            return row, s
    raise SourceError(f"스킬이 없습니다: {ident} — 목록(list)의 id를 쓰세요.")


# ---------------------------------------------------------------- 입력 → 조건

def resolve(screen: dict, params: dict | None) -> tuple[dict, dict]:
    """조건의 {자리}를 입력으로 — (봇이 돌 화면, 쓴 입력). 값이 빈 자리의 조건은 빼고(화면 기본값) 돈다."""
    skill = screen["skill"]
    params = params if isinstance(params, dict) else {}
    names = {p["name"] for p in skill["params"]}
    unknown = sorted(set(params) - names)
    if unknown:
        raise SourceError(f"이 스킬에 없는 입력입니다: {', '.join(unknown)} (입력: {', '.join(sorted(names)) or '없음'})")
    values = {}
    for p in skill["params"]:
        raw = params.get(p["name"])
        value = str(raw).strip()[:200] if raw is not None else ""
        value = value or p["default"]
        if not value and p["required"]:
            raise SourceError(f"입력 {p['name']}이 필요합니다 — {p['description'] or '값을 주세요'}")
        values[p["name"]] = value
    return fill(screen, values), values


def fill(screen: dict, values: dict) -> dict:
    """조건의 {자리}에 값을 넣은 화면(스킬 칸은 뺀다) — 값이 빈 자리의 조건은 뺀다(그 칸은 화면 기본값)."""
    conditions = []
    for c in screen["conditions"]:
        value = PLACEHOLDER.sub(lambda m: values.get(m[1], ""), c.get("value") or "")
        if value.strip() or not PLACEHOLDER.search(c.get("value") or ""):
            conditions.append({**c, "value": value})
    return {**{k: v for k, v in screen.items() if k != "skill"}, "conditions": conditions}


def with_defaults(screen: dict) -> dict:
    """수집(북마크·봇·서버 스캔)이 도는 화면 — 스킬의 {자리}를 기본값으로 채운다. 스킬이 없으면 그대로."""
    if not screen.get("skill"):
        return screen
    return fill(screen, {p["name"]: p["default"] for p in screen["skill"]["params"]})


# ---------------------------------------------------------------- 초안

DRAFT_PROMPT = """사내 업무 사이트의 조회 화면 하나를 **에이전트가 골라 부르는 스킬**로 정리한다. 에이전트는 목록에서
이름과 설명을 보고 고르고, 입력을 넣어 부르면 그 화면의 조회 결과를 받는다. 아래 사실에 있는 것만 쓴다 — 지어내지 마라.
출력은 JSON 객체 하나다(설명·코드펜스 없이).

{{"name": "짧은 이름(영문 소문자·숫자·-, 또는 한글 — 공백 없이)",
  "description": "이 스킬이 돌려주는 것 한두 문장(어떤 목록인지, 행 하나가 무엇인지)",
  "when": "에이전트가 이 스킬을 써야 하는 때 한 문장(어떤 질문에 답하나)",
  "params": [{{"field": "조회 칸 이름 — fields에 있는 그대로", "name": "입력 이름(짧게)", "description": "넣을 값 설명", "required": false}}],
  "outputs": [{{"name": "결과 열 이름 — columns에 있는 그대로", "description": "그 열의 뜻(모르면 빈 값)"}}]}}

규칙:
- params는 부를 때마다 바뀌는 칸만(협력사명·문서번호·담당자처럼 찾는 대상). 날짜 기간은 넣지 않는다(수집 조건이 정한다).
- 사실에 columns가 없으면 outputs는 빈 목록으로 둔다.

사실:
{facts}
"""


def draft(db: Session, row: InfoSource, url: str, screen: dict | None = None) -> dict:
    """화면 하나의 스킬 초안 — {"skill", "conditions", "via"}. LLM이 없거나 답이 틀리면 결정론 초안(조건 값이 상대
    날짜가 아닌 칸을 입력으로, 지난 수집의 열을 출력으로). 저장하지 않는다 — 사람이 고쳐 수집 조건과 함께 저장한다.
    screen을 주면 저장 전의 화면(정보 현황의 제안)으로 만든다."""
    screen = screen or next((s for s in infosource.query_screens(row) if s["url"] == url), None)
    if screen is None:
        raise SourceError("수집 조건에 없는 화면입니다 — 먼저 수집 조건에 화면을 더하고 저장하세요.")
    page = next((c for c in infosource.query_candidates(row) if c["url"] == url.split("#")[0] or c["url"] == url), {})
    columns = _columns(row, url) or [h for t in (page.get("tables") or [])[:1] for h in t][:MAX_OUTPUTS]
    fields = list(dict.fromkeys([c["field"] for c in screen["conditions"]] + list(page.get("fields") or [])))
    conditions = [dict(c) for c in screen["conditions"]]
    base = {"name": slug(screen["screen"]) or "skill",
            "description": (page.get("info") or f"{row.name}의 「{screen['screen']}」 조회 결과 목록")[:500],
            "when": "", "params": [], "outputs": [{"name": c, "description": ""} for c in columns]}
    for c in conditions:   # 결정론 — 상대 날짜(@…)가 아닌 칸은 부를 때 바꿀 입력으로
        value = c.get("value") or ""
        if value.startswith("@") or PLACEHOLDER.search(value):
            continue
        name = slug(c["field"]) or f"p{len(base['params']) + 1}"
        if any(p["name"] == name for p in base["params"]) or len(base["params"]) >= MAX_PARAMS:
            continue
        base["params"].append({"name": name, "description": f"「{c['field']}」 칸에 넣을 값", "default": value,
                               "required": False})
        c["value"] = "{" + name + "}"
    via = "rule"
    provider = llm.default_provider(db)
    if provider is not None:
        facts = {"source": row.name, "note": row.note[:300], "screen": screen["screen"],
                 "title": page.get("title") or "", "info": page.get("info") or "", "fields": fields,
                 "conditions": [{"field": c["field"], "fixed": bool(c.get("value"))} for c in screen["conditions"]],
                 "columns": columns}
        try:
            data = infosource._parse(llm.chat_completion(provider, [{"role": "user", "content": DRAFT_PROMPT.format(
                facts=json.dumps(facts, ensure_ascii=False, indent=1))}], db))
        except Exception:  # noqa: BLE001 — 초안은 결정론으로도 된다
            data = {}
        shaped = _from_llm(data, screen, fields, columns)
        if shaped:
            skill, conditions = shaped
            base, via = skill, "llm"
    return {"skill": base, "conditions": conditions, "via": via}


def _from_llm(data: dict, screen: dict, fields: list[str], columns: list[str]) -> tuple[dict, list[dict]] | None:
    """LLM 초안 — 사실에 있는 칸·열만 받는다. 입력으로 고른 칸은 조건 값을 {자리}로(지금 값은 기본값)."""
    name = slug(data.get("name"))
    if not name or not _txt(data.get("description"), 500):
        return None
    conditions = [dict(c) for c in screen["conditions"]]
    params = []
    for p in (data.get("params") if isinstance(data.get("params"), list) else [])[:MAX_PARAMS]:
        field, pname = _txt((p or {}).get("field"), 80), slug((p or {}).get("name"))
        if field not in fields or not pname or any(x["name"] == pname for x in params):
            continue
        hit = next((c for c in conditions if c["field"] == field and not (c.get("value") or "").startswith("@")), None)
        default = ""
        if hit is None:
            conditions.append({"field": field, "value": "{" + pname + "}"})
        else:
            default = "" if PLACEHOLDER.search(hit.get("value") or "") else hit.get("value") or ""
            hit["value"] = "{" + pname + "}"
        params.append({"name": pname, "description": _txt(p.get("description"), 200), "default": default,
                       "required": p.get("required") is True})
    known = set(columns)
    outputs = [{"name": _txt(o.get("name"), 80), "description": _txt(o.get("description"), 200)}
               for o in (data.get("outputs") if isinstance(data.get("outputs"), list) else [])
               if isinstance(o, dict) and _txt(o.get("name"), 80) in known][:MAX_OUTPUTS]
    return ({"name": name, "description": _txt(data.get("description"), 500), "when": _txt(data.get("when"), 300),
             "params": params, "outputs": outputs or [{"name": c, "description": ""} for c in columns]},
            conditions)


# ---------------------------------------------------------------- 실행

def _lock(source_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(source_id, threading.Lock())


def bot(row: InfoSource, screens: list[dict], *, timeout: float, step=lambda _t: None) -> dict:
    """출처 하나에 봇을 한 번 — 같은 출처는 한 번에 하나만(같은 세션으로 두 브라우저가 돌면 사이트가 끊는다)."""
    ok, why = runnable()
    if not ok:
        raise SourceError(f"서버에서 돌릴 수 없습니다 — {why}")
    lock = _lock(row.id)
    if not lock.acquire(blocking=False):
        raise SourceError(f"{row.name}에서 다른 실행이 돌고 있습니다 — 잠시 뒤 다시 부르세요.")
    try:
        return sourcebot.run({"id": row.id, "name": row.name, "version": infosource.queries_version(row)},
                             screens, infosource._headers(row),
                             start_url=next((s["start"] for s in screens if s.get("start")), row.url),
                             timeout=timeout, step=step)
    finally:
        lock.release()


def _file_text(f: dict) -> str:
    from . import doctext  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="paas-skill-") as tmp:
        path = Path(tmp) / f"file{f['ext']}"
        path.write_bytes(f["data"])
        try:
            return doctext.extract_markdown(path)[:MAX_TEXT]
        except Exception:  # noqa: BLE001 — 본문을 못 읽는 형식이면 이름만
            return ""


def _shape(result: dict, rows: int) -> dict:
    """봇 결과(infosource.from_browser 모양) → 에이전트가 읽을 결과. 목록은 열 이름(머리글)으로, 행은 rows개까지."""
    lists = []
    for d in result["datasets"]:
        label = {c["key"]: c["label"] or c["key"] for c in d["columns"]}
        lists.append({"screen": d["screen"], "columns": [label[c["key"]] for c in d["columns"]],
                      "rows": [{label.get(k, k): v for k, v in r.items()} for r in d["records"][:rows]],
                      "got": len(d["records"]), "total": d["total"], "complete": d["complete"]})
    pages = [{"title": c.get("title") or c.get("screen") or "", "url": c["url"],
              "markdown": (c.get("markdown") or "")[:MAX_TEXT]} for c in result["captures"][:5]]
    got, missed = infosource._download(result["downloads"], {}, result["origin"], limit=10)
    files = [{"name": f["name"], "size": len(f["data"]), "text": _file_text(f)} for f in got]
    return {"lists": lists, "pages": pages, "files": files,
            "notes": infosource._collected_notes(result["collected"])
            + [f"받지 않은 파일: {m['url']} — {m['reason']}" for m in missed[:5]]}


def run(db: Session, key: ApiKey, ident: str, params: dict | None = None, *, rows: int = MAX_ROWS) -> dict:
    """스킬 하나를 지금 돌려 결과를 돌려준다(저장소에 쓰지 않는다 — 저장은 데이터 수집의 수집이 한다)."""
    from .. import audit  # noqa: PLC0415

    row, screen = find(db, ident)
    if not can_use(db, key, screen["skill"]):
        raise SourceError(f"이 스킬을 부를 권한이 없습니다: {ident} — 관리자가 공개 범위에 조직을 더해야 합니다.")
    ran, values = resolve(screen, params)
    last = infosource.dataset_marks(row).get(screen["url"])
    if last:   # @last — 지난번에 빠짐없이 받은 날(served_screens와 같다)
        ran["last"] = last
    cache_key = hashlib.sha256(json.dumps([row.id, ran], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    hit = _CACHE.get(cache_key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return {**hit[1], "cached": True}
    started = time.monotonic()
    try:
        payload = bot(row, [ran], timeout=RUN_TIMEOUT)
        result = infosource.from_browser(row, payload)
    except SourceError as e:
        audit.record(db, key.name, "source.skill.run", skill_id(row, screen["skill"]["name"]),
                     {"ok": False, "params": sorted(values), "error": str(e)[:300]})
        raise
    out = {"skill": skill_id(row, screen["skill"]["name"]), "screen": screen["screen"],
           "params": values, **_shape(result, max(1, min(rows, MAX_ROWS))),
           "seconds": round(time.monotonic() - started, 1), "cached": False}
    landed = payload.get("landed") or {}
    if landed.get("url") and not out["lists"] and not out["files"] and not out["pages"]:
        out["notes"].append(f"서버 브라우저의 첫 화면: {landed.get('title') or '(제목 없음)'} · {landed['url']}")
    _CACHE[cache_key] = (time.monotonic(), out)
    for k in [k for k, (at, _) in _CACHE.items() if time.monotonic() - at >= CACHE_SECONDS]:
        _CACHE.pop(k, None)
    audit.record(db, key.name, "source.skill.run", out["skill"],
                 {"ok": True, "params": sorted(values), "rows": sum(x["got"] for x in out["lists"]),
                  "files": len(out["files"]), "pages": len(out["pages"]), "seconds": out["seconds"],
                  # 메모는 화면 이름·막힌 까닭뿐이다(입력값은 싣지 않는다) — 하네스가 0건의 까닭으로 읽는다
                  "notes": [n[:200] for n in out["notes"][:3]]})
    return out


# ---------------------------------------------------------------- 대화로 정리

MAX_CHAT_TURNS = 20
MAX_CHAT_CHARS = 4000
CHAT_SYSTEM = """너는 사내 업무 사이트의 크롤링 결과를 **수집 스킬**로 정리하는 관리자를 돕는다. 스킬은 조회 화면 하나를
에이전트가 골라 부르는 일로 묶은 것이다 — 이름·용도(description)·쓸 때(when)·입력(params)·결과 열(outputs).
입력은 조건 값의 {이름} 자리다(부를 때 값을 받고, 수집은 기본값으로 돈다). 날짜 기간은 입력으로 두지 않고 조건에
@last|@today-3y · @today 같은 상대 날짜로 둔다.

아래 '사실'(지금 편집 중인 화면과 스킬, 더할 수 있는 화면)에 있는 것만 쓴다 — 칸(fields)·결과 열(columns)을
지어내지 마라. 무엇을 원하는지 모호하면 바꾸지 말고 reply로 묻는다. 관리자가 바꾸라고 하거나 동의한 것만 changes에
넣는다(바꾸지 않을 화면은 넣지 않는다). 출력은 JSON 객체 하나다(설명·코드펜스 없이).

{"reply": "관리자에게 하는 말(한국어, 짧게 — 무엇을 왜 바꿨는지 또는 묻는 것)",
 "changes": [{"url": "화면 url 그대로",
              "conditions": [{"field": "칸", "value": "값 또는 {입력이름}"}],
              "skill": {"name": "", "description": "", "when": "",
                        "params": [{"name": "", "description": "", "default": "", "required": false}],
                        "outputs": [{"name": "", "description": ""}]}}]}

- conditions는 바꿀 때만 넣는다(빼면 그대로). skill을 null로 주면 그 화면의 스킬을 뺀다.
- 공개 범위(누가 부르나)는 관리자가 화면에서 고른다 — 정하지 않는다.
"""


def _facts_for_chat(row: InfoSource, screens: list[dict]) -> dict:
    cands = {c["url"]: c for c in infosource.query_candidates(row)}
    collected = {c.get("url"): c for c in (row.scan or {}).get("collected") or []}
    out = []
    for s in screens:
        page = cands.get(s["url"]) or cands.get(s["url"].split("#")[0]) or {}
        last = collected.get(s["url"]) or {}
        out.append({"url": s["url"], "screen": s["screen"], "conditions": s["conditions"],
                    "skill": {k: v for k, v in (s.get("skill") or {}).items() if k != "audience"} or None,
                    "fields": list(dict.fromkeys([c["field"] for c in s["conditions"]] + list(page.get("fields") or []))),
                    "columns": _columns(row, s["url"]) or [h for t in (page.get("tables") or [])[:1] for h in t],
                    # 다시 받아 본 건에서 열마다 바뀐 비율 — 고정(신원·기준)과 변경(상태)을 가르는 근거
                    "volatility": {v["name"]: v["hint"] for v in infosource.column_volatility(row.id, s["url"])},
                    "info": page.get("info") or "",
                    "last_collect": {k: last[k] for k in ("rows", "details", "files", "notes") if k in last} or None})
    used = {s["url"] for s in screens}
    more = [{"url": c["url"], "title": c["title"], "fields": c["fields"], "tables": c.get("tables") or []}
            for c in cands.values() if c["url"] not in used][:30]
    return {"source": row.name, "note": row.note[:300], "screens": out, "can_add": more}


def chat(db: Session, row: InfoSource, messages: list[dict], screens) -> dict:
    """스킬을 대화로 정리한다 — {"reply", "changes", "notes"}. **저장하지 않는다**: 화면이 고칠 것(changes)을 편집
    중인 수집 조건에 적용하고, 사람이 저장한다. 받은 것은 사실에 맞는지 다시 본다(칸·자리·이름 — clean)."""
    provider = llm.default_provider(db)
    if provider is None:
        raise SourceError("기본 LLM 프로바이더가 없어 대화로 정리할 수 없습니다 — 스킬은 직접 고쳐 쓸 수 있습니다.")
    draft = infosource.clean_screens(row, screens) if screens else infosource.query_screens(row)
    turns = [{"role": m["role"], "content": str(m.get("content") or "")[:MAX_CHAT_CHARS]}
             for m in (messages if isinstance(messages, list) else [])[-MAX_CHAT_TURNS:]
             if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    if not turns or turns[-1]["role"] != "user":
        raise SourceError("보낼 말이 없습니다.")
    facts = json.dumps(_facts_for_chat(row, draft), ensure_ascii=False)[:40000]
    text = llm.chat_completion(provider, [{"role": "system", "content": CHAT_SYSTEM + "\n사실:\n" + facts}, *turns], db)
    data = infosource._parse(text)
    if not data:
        return {"reply": (text or "").strip()[:2000] or "답을 읽지 못했습니다 — 다시 말해 주세요.", "changes": [],
                "notes": []}
    changes, notes = _changes_in(row, draft, data.get("changes"))
    return {"reply": _txt(data.get("reply"), 2000), "changes": changes, "notes": notes}


def _declare_missing(skill: dict, conditions: list[dict]) -> list[str]:
    """LLM이 조건에 {자리}를 쓰고 입력으로 정의하지 않았으면(또는 다른 이름으로 정의했으면) 그 자리를 입력으로 더한다 —
    설명은 그 칸 이름으로. 더한 이름들을 돌려준다(사람이 쓴 것은 고치지 않는다 — clean이 오류로 알린다)."""
    params = skill["params"] = [p for p in skill.get("params") or [] if isinstance(p, dict)]
    have = {slug(p.get("name")) for p in params}
    added = []
    for c in conditions:
        for m in PLACEHOLDER.findall(c.get("value") or ""):
            if m not in have and len(params) < MAX_PARAMS:
                params.append({"name": m, "description": f"「{c['field']}」 칸에 넣을 값", "default": "", "required": False})
                have.add(m)
                added.append(m)
    return added


def _changes_in(row: InfoSource, draft: list[dict], raw) -> tuple[list[dict], list[str]]:
    """LLM이 낸 고칠 것 — 편집 중인 화면이거나 더할 수 있는 화면만, 칸은 그 화면의 칸만, 스킬은 clean을 거친 것만."""
    by_url = {s["url"]: s for s in draft}
    cands = {c["url"]: c for c in infosource.query_candidates(row)}
    out, notes = [], []
    for ch in (raw if isinstance(raw, list) else [])[:infosource.MAX_QUERY_SCREENS]:
        url = _txt((ch or {}).get("url"), 1024) if isinstance(ch, dict) else ""
        base = by_url.get(url)
        cand = cands.get(url)
        if base is None and cand is None:
            notes.append(f"모르는 화면이라 뺐습니다: {url[:100]}")
            continue
        fields = set((cand or {}).get("fields") or []) | {c["field"] for c in (base or {}).get("conditions") or []}
        conditions = (base or {}).get("conditions") or []
        if isinstance(ch.get("conditions"), list):
            asked = [{"field": _txt(c.get("field"), 80), "value": _txt(c.get("value"), 200)}
                     for c in ch["conditions"][:infosource.MAX_CONDITIONS] if isinstance(c, dict)]
            bad = sorted({c["field"] for c in asked if c["field"] not in fields})
            if bad:
                notes.append(f"{(base or cand).get('screen') or (cand or {}).get('title') or url}: 화면에 없는 칸이라 조건을 "
                             f"바꾸지 않았습니다 — {', '.join(bad)}")
            else:
                conditions = [c for c in asked if c["field"]]
        change = {"url": url, "conditions": conditions, "new": base is None}
        if base is None:
            change.update({"screen": cand.get("title") or url,
                           **{k: cand[k] for k in ("path", "start", "frame") if cand.get(k)}})
        if "skill" in ch and ch["skill"] is None:
            change["skill"] = None
        elif isinstance(ch.get("skill"), dict):
            raw_skill = {**ch["skill"], "audience": ((base or {}).get("skill") or {}).get("audience") or []}
            added = _declare_missing(raw_skill, conditions)
            if added:
                notes.append(f"{raw_skill.get('name') or url}: 조건에 쓴 자리를 입력으로 더했습니다 — {', '.join(added)}"
                             "(설명·기본값은 카드에서 고치세요)")
            try:
                skill = clean(raw_skill, conditions)
            except SourceError as e:
                notes.append(str(e))
                continue
            if skill:
                change["skill"] = skill
        out.append(change)
    return out, notes
