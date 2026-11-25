"""내용 온톨로지 — 문서의 **형식**이 아니라 **내용**에서 개념과 관계를 뽑는다(LLM).

형식 그래프(services/ontology.py)는 제목·조문·표 머리글·「」 인용에서 나온다. 사실이지만, 같은 내용이라도
제목 스타일이나 표가 없으면 노드가 없고 "A사가 B 품목을 공급한다" 같은 관계는 아예 없다. 이 모듈이 그
관계를 만든다. 형식 그래프는 그대로 둔다 — 그것을 쓰는 곳(워크플로 검증·연결, 전환 현황)이 종류 4가지를
전제로 하고, LLM이 없거나 실패해도 남아야 한다. 저장은 같은 색인 파일의 따로 된 표다(sem_*).

네 단계이고, **한 번 정해 많은 문서에 퍼지는 결과는 LLM이 전부 검토**하고 문서 하나에 그치는 결과는 표본만
검토한다(모든 결과를 다시 읽으면 비용이 두 배다):

  ① 스키마   표본 문서 → 개념 종류 제안 → 검토 → 고침 → 관계는 개념 몇 개씩 묶어 차례로 제안 → 묶음마다 검토
             → 관리자 확정(판 번호가 생긴다). 관계 수에 상한을 두지 않는다
  ②-1 양식   되풀이되는 표(견적서 양식 하나가 320개 문서에 있다)는 LLM이 문서마다 읽지 않는다. 열이 어떤
             개념인지 대응표를 제안 → 실제 값 표본과 대조해 검토 → 관리자 확정 → **규칙으로** 전부 뽑는다
  ③ 문서     나머지 문서 → 추출 → 기계 검사(전부: 근거 문장이 원문에 있나·스키마에 있나) → 표본 검토(10%)
             → 표본 오류율이 기준을 넘으면 멈추고 알린다(스키마·지시가 틀린 것이라 계속 돌면 비용만 든다)
  ④ 묶기     이름 정규화로 같은 것을 묶고, 애매한 것은 LLM이 묶음을 제안 → 묶음마다 검토 → 통과한 것만 적용

제안과 검토를 같은 기본 LLM이 한다 — 같은 모델은 같은 실수를 놓칠 수 있어서, 원문·실제 값과 기계로
대조하는 검사를 함께 둔다(검토가 놓쳐도 그것은 걸린다).
"""
import csv
import hashlib
import io
import json
import re
import threading
import time
from datetime import date, datetime, timezone
from html import unescape

from sqlalchemy.orm import Session

from ..config import get_settings
from . import docsearch, docready, ontology
from . import semantic_template as st
from . import llm as llm_service
from . import storage as storage_service

# 표본·추출에 넣는 문서 길이. 문서 전체를 넣으면 긴 규정 하나가 수십만 토큰이다 — 앞부분이 대개 목적·정의·
# 당사자를 말한다. 잘렸다는 사실은 기록한다(truncated).
SAMPLE_CHARS = 2_500
DOC_CHARS = 12_000
# 분석하지 않는 문서 — 업무 내용이 아니라 작업 파일(로그·설정·코드)이거나, 읽을 글이 거의 없는 것(제목만 남은 슬라이드).
# 문서마다 LLM 한 번이라 6천 건이면 수천만 토큰이다 — 뽑을 것이 없는 문서에 쓰지 않는다.
SKIP_EXTENSIONS = {".log", ".json", ".xml", ".yml", ".yaml", ".ini", ".cfg", ".conf", ".toml", ".env",
                   ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".ps1", ".bat", ".cmd", ".sh", ".sql"}
MIN_CHARS = 100
SAMPLE_DOCS = 12
# 이만큼 많은 문서에 되풀이되는 표가 "양식"이다.
FORM_MIN_DOCS = 5
FORM_SAMPLE_DOCS = 3
# 확정한 양식 표를 뺀 나머지 글이 이보다 짧으면 "양식 문서"다(견적서) — LLM을 부르지 않는다. 길면(신용평가서처럼
# 양식 표 몇 개를 품은 보고서) 나머지 글은 LLM이 읽는다. 문서 전체를 건너뛰면 그 본문의 관계가 통째로 빠진다(실측).
FORM_REST_CHARS = 1_500
FORM_SAMPLE_ROWS = 8
# 템플릿(②-2) — 같은 틀의 문서가 이만큼이면 규칙을 짤 만하다. 규칙은 표본 몇 건(정답은 LLM 추출)으로 짜고 **다른**
# 몇 건으로 검증한다. 검증 정밀도가 기준을 넘어야 확정할 수 있다.
TEMPLATE_MIN_DOCS = 5
TEMPLATE_TRAIN_DOCS = 3
TEMPLATE_CHECK_DOCS = 3
TEMPLATE_PRECISION = 0.9
TEMPLATE_SAMPLE_CHARS = 4_000
# 템플릿 규칙이 뽑은 항목에 사람 정답이 이만큼 쌓이면 그 정밀도로도 잰다(기준 다시 배우기).
TEMPLATE_GOLD_MIN = 10
GOLD_KINDS = ("pair", "node", "edge")
# 표본 검토 — 10%. 경로의 해시로 고른다(돌 때마다 같은 문서가 고르게 뽑힌다).
REVIEW_EVERY = 10
# 멈춤 조건 — 표본으로 이만큼 본 뒤에 틀린 비율이 기준을 넘으면.
STOP_MIN_ITEMS = 30
STOP_ERROR_RATE = 0.2
# 관계는 한 번에 다 찾지 않는다 — 개념을 이만큼씩 묶어(그 개념에서 나가는 관계) 차례로 제안·검토한다. 관계 수에
# 상한을 두지 않는다: 업무가 넓은 저장소는 관계가 많고, 한 번에 다 내게 하면 상한에 걸리거나(실측: 40개 초과로
# 확정 불가) 뒤쪽이 대충 채워진다.
RELATION_BATCH = 4
# 층위 — 고정부(지식)와 변경부(정보)를 **뜻의 역할 × 변화 속도**로 가른다(docs/ontology-layers.md).
#   reference  기준 지식 — 규정·용어·업무절차·코드·기준값. 느리게, 시행일과 개정으로 바뀐다
#   entity     대상(마스터) — 협력사·품목·부서·사람. 신원은 거의 바뀌지 않는다(묶기의 근거)
#   state      상태 — 신용등급·계약상태·재무위험. 관측 시점이 있어야 뜻이 있다
#   event      사건 — 계약·견적·발주·평가. 일어난 때가 있다
# 구조(TBox)는 이 스키마 자신이다(판 번호). 상태·사건과 그것에 닿는 관계는 시간에 매인다(temporal).
LAYERS = {"reference": "기준 지식", "entity": "대상", "state": "상태", "event": "사건"}
TEMPORAL_LAYERS = {"state", "event"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sem_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sem_docs (
  path TEXT PRIMARY KEY, fp TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
  truncated INTEGER NOT NULL DEFAULT 0, dropped INTEGER NOT NULL DEFAULT 0,
  reviewed INTEGER NOT NULL DEFAULT 0, analyzed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sem_nodes (
  path TEXT NOT NULL, cls TEXT NOT NULL, name TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'llm', observed_at TEXT NOT NULL DEFAULT ''
);
-- observed_at: 그 사실을 본 날(문서의 날짜·목록의 수집일) · valid_from/valid_to: 문서가 말한 효력 기간.
-- 상태·사건 관계(temporal)는 문서마다 하나의 **관측**이다 — 갈아 끼워도 다른 문서의 관측은 남아 이력이 된다.
CREATE TABLE IF NOT EXISTS sem_edges (
  path TEXT NOT NULL, src_cls TEXT NOT NULL, src TEXT NOT NULL, rel TEXT NOT NULL,
  dst_cls TEXT NOT NULL, dst TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'llm', observed_at TEXT NOT NULL DEFAULT '',
  valid_from TEXT NOT NULL DEFAULT '', valid_to TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sem_aliases (
  cls TEXT NOT NULL, alias TEXT NOT NULL, canonical TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (cls, alias)
);
CREATE INDEX IF NOT EXISTS ix_sem_nodes_path ON sem_nodes(path);
CREATE INDEX IF NOT EXISTS ix_sem_nodes_name ON sem_nodes(cls, name);
CREATE INDEX IF NOT EXISTS ix_sem_edges_path ON sem_edges(path);
CREATE INDEX IF NOT EXISTS ix_sem_edges_src ON sem_edges(src_cls, src);
CREATE INDEX IF NOT EXISTS ix_sem_edges_dst ON sem_edges(dst_cls, dst);
-- 묶기의 짝 결정(semantic_merge) — 같은 짝을 다시 묻지 않고, 사람이 「풀기」한 짝은 다시 묶지 않는다.
CREATE TABLE IF NOT EXISTS sem_pairs (
  cls TEXT NOT NULL, a TEXT NOT NULL, b TEXT NOT NULL, decision TEXT NOT NULL, source TEXT NOT NULL,
  score REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', decided_at TEXT NOT NULL,
  PRIMARY KEY (cls, a, b)
);
-- 사람이 확인한 정답 — 짝(pair)·개체(node)·관계(edge)가 맞는지. 기준을 다시 배우고 재는 근거다(semantic_tune).
CREATE TABLE IF NOT EXISTS sem_gold (
  kind TEXT NOT NULL, item TEXT NOT NULL, label INTEGER NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
  labeled_by TEXT NOT NULL DEFAULT '', labeled_at TEXT NOT NULL,
  PRIMARY KEY (kind, item)
)
"""


class SemanticError(RuntimeError):
    """사람이 읽을 사유 — 화면에 그대로 뜬다."""


# 옛 색인 파일의 표에는 없던 열 — CREATE TABLE IF NOT EXISTS는 열을 더하지 않는다(docsearch._add_missing_columns와 같다)
_ADDED = (("sem_nodes", "observed_at"), ("sem_edges", "observed_at"), ("sem_edges", "valid_from"),
          ("sem_edges", "valid_to"),
          # LLM 추출의 기계 검사 — 근거 문장에 이름이 있었나('1'·'0', 규칙으로 뽑은 것은 ''). ⑤ 사람 확인이 불확실한 것부터
          # 묻는 근거다(예전에는 저장하지 않아 개체·관계를 무작위로만 물었다)
          ("sem_nodes", "anchored"), ("sem_edges", "anchored"))


def _anchored(item: dict) -> str:
    v = item.get("anchored")
    return "" if v is None else ("1" if v else "0")


# 내용 분석의 연결은 쓰기를 30초까지 기다린다(색인 DB 기본 5초). 색인 DB는 WAL이라 읽기는 기다리지 않는다 — 기다리는 것은
# 다른 쓰기(분석·묶기·색인·확정)가 끝나기를이고, 그런 쓰기는 짧다. 5초로는 겹치면 묶기·기준 배우기처럼 오래 도는 작업이
# 「database is locked」로 통째로 실패했다(운영: intro 묶기 2시간 반 뒤 · contracts 기준 배우기).
SEMANTIC_DB_TIMEOUT = 30.0


def _conn(store_name: str, timeout: float = SEMANTIC_DB_TIMEOUT):
    conn = docsearch._connect(store_name, timeout=timeout)
    conn.executescript(_SCHEMA)
    for table, column in _ADDED:
        if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            conn.commit()
    return conn


_DATE = re.compile(r"(20\d{2}|19\d{2})\s*[-./년]?\s*(\d{1,2})\s*[-./월]?\s*(\d{1,2})\s*일?")
_NAME_DATE = re.compile(r"(?<!\d)(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")


def iso_date(value) -> str:
    """2024-01-05 · 2024.1.5 · 2024년 1월 5일 → 2024-01-05. 날짜가 아니면 ""."""
    m = _DATE.search(str(value or ""))
    if not m:
        return ""
    try:
        return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
    except ValueError:
        return ""


# 연도만 적힌 이름 — 「22년 신용평가」·「#24년 실적」·「2024년」. 범위(「23-25년」·「23~25년」)는 어느 해인지 몰라 읽지 않는다.
_NAME_YEAR = re.compile(r"(?<![\d~\-])((?:19|20)\d{2}|\d{2})\s*년")
# 폴더 이름 전체가 연도·연월인 것 — 「2024」·「2026-10」(work-square KPI/2024, dream-rsi audit/2026-10)
_FOLDER_YM = re.compile(r"^((?:19|20)\d{2})(?:[-_.](0[1-9]|1[0-2]))?$")


def _name_date(name: str, mdate: date | None, *, folder: bool) -> str:
    m = _NAME_DATE.search(name)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            pass
    year = month = None
    ym = _FOLDER_YM.match(name.strip()) if folder else None
    if ym:
        year, month = int(ym[1]), int(ym[2]) if ym[2] else None
    elif (y := _NAME_YEAR.search(name)):
        year = int(y[1])
        if year < 100:      # 두 자리 — 올해(+1)까지는 2000년대, 넘으면 1900년대
            year += 2000 if year <= date.today().year % 100 + 1 else 1900
    if year is None:
        return ""
    # 수정일이 그 해(달) 안이면 수정일이 더 정확하다. 아니면 그 해(달)의 첫날 — 복사·이관으로 수정일이 한날로 뭉개져도
    # 해의 순서(22년 → 23년 → 24년 신용평가)는 지킨다.
    if mdate and mdate.year == year and (month is None or mdate.month == month):
        return mdate.isoformat()
    return date(year, month or 1, 1).isoformat()


def doc_date(path: str, mtime: float | None) -> str:
    """문서의 날 — 관측 시점. 파일 이름의 날짜(메일·수집 파일은 이름에 날짜가 있다) → 파일 이름의 연도(「22년 신용평가.pdf」)
    → 가까운 폴더부터 폴더 이름의 날짜·연월·연도(「WPR(팀)/2024년/…」) → 파일의 수정일.

    연도를 읽지 않던 때는 신용평가서(협력사마다 22·23·24년 파일)가 모두 수정일 — 한꺼번에 복사한 날 — 로 관측돼 「가장
    늦게 본 값」이 해의 순서와 어긋났다(실측: credit 467건이 「YY년 신용평가」)."""
    mdate = datetime.fromtimestamp(mtime, timezone.utc).date() if mtime else None
    for i, name in enumerate(reversed(path.split("/"))):
        got = _name_date(name, mdate, folder=i > 0)
        if got:
            return got
    return mdate.isoformat() if mdate else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM sem_meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def _put(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO sem_meta (key, value) VALUES (?, ?)",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


def _digest(value) -> str:
    return hashlib.sha1(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


# --- LLM ---

def provider_for(db: Session, store_name: str):
    """기본 LLM. 조직 범위는 보지 않는다 — 내용 분석은 관리자만 시작하고(제안·확정·분석·묶기 모두 관리자 창구),
    관리자는 모든 조직의 프로바이더를 쓴다(다른 LLM 사용과 같은 규칙, llm.require_provider_access)."""
    provider = llm_service.default_provider(db)
    if provider is None:
        raise SemanticError("기본 LLM 프로바이더가 없습니다 — LLM 관리에서 기본값을 정하세요.")
    return provider


def _json_of(text: str):
    """응답에서 JSON을 꺼낸다 — ```json 블록이 있으면 그것, 없으면 첫 { 부터 짝이 맞는 } 까지."""
    block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = block.group(1) if block else text[text.find("{"): text.rfind("}") + 1]
    try:
        # strict=False — 모델이 문자열 안에 줄바꿈을 그대로 넣는 일이 있다(근거 문장을 옮기다가, 실측). 엄밀히는
        # JSON이 아니지만 뜻은 분명하다 — 그 한 글자로 문서 하나를 실패로 두지 않는다.
        return json.loads(raw, strict=False)
    except ValueError as e:
        raise SemanticError(f"LLM 응답이 JSON이 아닙니다: {text[:200]!r}") from e


def _ask(db: Session, provider, system: str, user: str) -> dict:
    out = llm_service.chat_completion(
        provider, [{"role": "system", "content": system}, {"role": "user", "content": user}], db)
    data = _json_of(out)
    if not isinstance(data, dict):
        raise SemanticError("LLM 응답이 JSON 객체가 아닙니다.")
    return data


_KO = "모든 이름과 설명은 한국어로. 응답은 JSON 하나만(설명 문장 없이)."


def _quiet(text: str, step: int | None = None, steps: int | None = None) -> None:
    """진행 보고의 기본값 — 뒤에서 돌 때는 submit이 task에 남기는 함수로 바꾼다(화면 트레이가 읽는다)."""


# --- 문서 읽기 ---

def _paths(conn) -> list[str]:
    return [r["path"] for r in conn.execute("SELECT path FROM docs WHERE body IS NOT NULL ORDER BY path")]


def _root(store_name: str):
    store = storage_service.store(store_name)
    if store is None:
        raise SemanticError(f"저장소가 없습니다: {store_name}")
    return store.root


def _markdown(store_name: str, rel: str, root=None) -> str:
    return docready.read(store_name, rel, (root or _root(store_name)) / rel)


def _list_body(store_name: str, rel: str, root=None) -> str:
    """목록 CSV는 원본 파일에서 읽는다 — 색인 본문은 길이 상한에서 잘린다(목록은 수만 행일 수 있다)."""
    raw = ((root or _root(store_name)) / rel).read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --- ① 스키마 ---

def _form_tables(conn) -> list[dict]:
    """되풀이되는 표(양식) — 표 머리글이 같은 문서가 FORM_MIN_DOCS 이상. 데이터 수집이 저장한 목록(… 목록.csv)도
    양식이다(문서는 하나지만 행마다 같은 모양의 건이다) — kind=list, 이름은 「목록 · 경로」."""
    tables = [{"name": r["name"], "columns": r["name"].split(" | "), "documents": r["n"], "kind": "table"}
              for r in conn.execute(
                  "SELECT name, COUNT(DISTINCT path) AS n FROM nodes WHERE kind = 'table'"
                  " GROUP BY name HAVING n >= ? ORDER BY n DESC, name", (FORM_MIN_DOCS,))]
    return tables + [{"name": LIST_PREFIX + r["path"], "columns": r["columns"], "documents": 1, "kind": "list",
                      "path": r["path"]} for r in _list_docs(conn)]


LIST_SUFFIX = " 목록.csv"     # infosource.merge_dataset이 저장하는 이름
LIST_PREFIX = "목록 · "
SEEN_DATE = "수집 바뀜"        # infosource.SEEN_COLUMNS[1] — 그 건이 마지막으로 바뀐 날
LIST_MAX_ROWS = 20_000


def _list_rows(body: str) -> tuple[list[str], list[dict]]:
    """목록 CSV 본문 → (머리글, 행들)."""
    reader = csv.reader(io.StringIO(body.lstrip("\ufeff")))
    header = next(reader, [])
    rows = [{h: (cells[k] if k < len(cells) else "") for k, h in enumerate(header)}
            for cells, _ in zip(reader, range(LIST_MAX_ROWS))]
    return header, rows


def _list_docs(conn) -> list[dict]:
    out = []
    for r in conn.execute("SELECT path, body FROM docs WHERE body IS NOT NULL AND path LIKE ?", (f"%{LIST_SUFFIX}",)):
        header = _list_rows((r["body"] or "").split("\n", 1)[0])[0]
        if header:
            out.append({"path": r["path"], "columns": header})
    return out


def _sample(store_name: str, conn, n: int = SAMPLE_DOCS) -> list[dict]:
    """표본 문서 — 양식은 하나씩만(320건짜리 견적서가 표본을 다 먹지 않게), 나머지는 큰 것부터 고르게."""
    forms = {f["name"] for f in _form_tables(conn)}
    form_docs: dict[str, str] = {}
    for r in conn.execute("SELECT path, name FROM nodes WHERE kind = 'table'"):
        if r["name"] in forms:
            form_docs.setdefault(r["path"], r["name"])
    seen_forms: set[str] = set()
    picked: list[str] = []
    # 데이터 수집 목록(… 목록.csv)은 표본에서 뺀다 — 길어서 큰 것부터 고르면 표본을 먹고, 그 행은 ②-1 대응표로 뽑는다
    rows = list(conn.execute("SELECT path FROM docs WHERE body IS NOT NULL AND path NOT LIKE ?"
                             " ORDER BY length(body) DESC", (f"%{LIST_SUFFIX}",)))
    step = max(1, len(rows) // max(1, n * 2))
    for r in rows[::step] + rows:
        path = r["path"]
        form = form_docs.get(path)
        if path in picked or (form and form in seen_forms):
            continue
        if form:
            seen_forms.add(form)
        picked.append(path)
        if len(picked) >= n:
            break
    out = []
    for path in picked:
        try:
            text = _markdown(store_name, path)
        except Exception:  # noqa: BLE001 — 표본 하나가 안 읽혀도 나머지로 간다
            continue
        out.append({"path": path, "text": text[:SAMPLE_CHARS]})
    return out


def _format_hints(conn) -> dict:
    """지금 형식 그래프가 아는 것 — 되풀이 용어·표 머리글·제목. LLM이 문서만 보고 놓칠 반복을 알려 준다."""
    def top(kind: str, limit: int) -> list[str]:
        return [r["name"] for r in conn.execute(
            "SELECT name, COUNT(DISTINCT path) AS n FROM nodes WHERE kind = ? GROUP BY name"
            " ORDER BY n DESC, name LIMIT ?", (kind, limit))]
    return {"terms": top("term", 40), "sections": top("section", 40), "tables": top("table", 20)}


_CLASS_RULES = (
    "개념 종류(classes) — 이 저장소의 업무에 실제로 등장하는 대상만(예: 협력사·품목·계약·부서·규정·업무절차)."
    " 문서 형식(절·표·문단)은 개념이 아니다. 같은 것을 두 이름으로 나누지 말 것.\n"
    "개념마다 층위(layer)를 정한다 — reference(기준 지식: 규정·용어·절차·코드·기준값처럼 드물게 개정되는 것),"
    " entity(대상: 협력사·품목·부서·사람처럼 신원이 있는 것), state(상태: 등급·상태·위험처럼 시점마다 값이 바뀌는 것),"
    " event(사건: 계약·견적·발주·평가처럼 일어난 때가 있는 것).\n"
    'JSON: {"classes":[{"name":"","description":"","layer":"reference|entity|state|event","examples":[""]}]}'
)
_RELATION_RULES = (
    "관계 종류 — from·to는 개념 목록의 이름이고 방향이 뜻이다(협력사 —공급한다→ 품목). 표본에 근거가 있는 것만."
    " 이미 정한 관계와 뜻이 같으면 새 이름을 만들지 말고 **그 이름을 그대로** 쓴다(승인받는다·결재받는다를 둘로"
    " 두지 않는다). temporal은 그 관계가 시점에 매이면 true(등급이 바뀐다·계약 기간이 있다), 늘 같으면 false.\n"
    'JSON: {"relations":[{"name":"","from":"","to":"","description":"","temporal":false}]}'
)


def _samples_text(samples: list[dict]) -> str:
    return "\n\n".join(f"### {s['path']}\n{s['text']}" for s in samples)


def check_schema(schema: dict) -> list[str]:
    """스키마의 기계 검사 — 이름 중복, 관계의 끝이 개념 목록에 있는지. **개수 상한은 두지 않는다**(RELATION_BATCH)."""
    problems = []
    classes = [c.get("name", "").strip() for c in schema.get("classes", []) if isinstance(c, dict)]
    if not classes:
        problems.append("개념 종류가 없습니다.")
    if len(set(classes)) != len(classes):
        problems.append("개념 이름이 겹칩니다.")
    for r in schema.get("relations", []):
        if not isinstance(r, dict) or not r.get("name") or r.get("from") not in classes or r.get("to") not in classes:
            problems.append(f"관계 '{r.get('name') if isinstance(r, dict) else r}'의 from/to가 개념 목록에 없습니다.")
    return problems


def _clean_classes(classes) -> list[dict]:
    """층위가 없거나 모르는 값이면 entity(대상) — 옛 스키마도 그대로 읽힌다."""
    return [{"name": str(c["name"]).strip(), "description": str(c.get("description", ""))[:300],
             "layer": c.get("layer") if c.get("layer") in LAYERS else "entity",
             "examples": [str(x)[:60] for x in (c.get("examples") or [])][:5]}
            for c in (classes or []) if isinstance(c, dict) and c.get("name")]


def _clean_relations(relations, classes: list[dict] | None = None) -> list[dict]:
    """temporal을 적지 않았으면 끝의 개념 층위로 — 상태·사건에 닿는 관계는 시점에 매인다."""
    layer = {c["name"]: c["layer"] for c in classes or []}
    out = []
    for r in relations or []:
        if not isinstance(r, dict) or not r.get("name"):
            continue
        a, b = str(r.get("from", "")).strip(), str(r.get("to", "")).strip()
        temporal = r["temporal"] if isinstance(r.get("temporal"), bool) else bool(
            {layer.get(a), layer.get(b)} & TEMPORAL_LAYERS)
        out.append({"name": str(r["name"]).strip(), "from": a, "to": b,
                    "description": str(r.get("description", ""))[:300], "temporal": temporal})
    return out


def _clean_schema(schema: dict) -> dict:
    classes = _clean_classes(schema.get("classes"))
    return {"classes": classes, "relations": _clean_relations(schema.get("relations"), classes)}


def layer_of(schema: dict) -> dict[str, str]:
    """개념 → 층위(옛 스키마의 개념은 entity)."""
    return {c["name"]: c.get("layer") if c.get("layer") in LAYERS else "entity" for c in schema.get("classes") or []}


def temporal_rels(schema: dict) -> set[str]:
    """시점에 매인 관계 이름 — 조회가 최신(current)을 가리고 기준 시점(as_of)으로 거른다."""
    layers = layer_of(schema)
    return {r["name"] for r in _clean_relations(schema.get("relations"), [
        {"name": k, "layer": v} for k, v in layers.items()]) if r["temporal"]}


def propose_schema(db: Session, store_name: str, report=_quiet) -> dict:
    """① 스키마를 제안한다 — 확정은 하지 않는다.

    개념 종류를 먼저 정하고(제안 → LLM 검토 → 고침), 관계는 **개념 몇 개씩(RELATION_BATCH) 묶어 차례로** 찾는다 —
    묶음마다 그 개념에서 나가는 관계를 제안하고 LLM이 검토한다. 뒤 묶음은 앞에서 정한 관계를 보고 같은 뜻이면 그
    이름을 쓴다(이름이 갈리지 않게). 관계 수에 상한은 없다."""
    provider = provider_for(db, store_name)
    conn = _conn(store_name)
    try:
        report("표본 문서를 고르는 중", 1, 4)
        samples = _sample(store_name, conn)
        if not samples:
            raise SemanticError("읽을 수 있는 문서가 없습니다 — 먼저 색인하세요.")
        hints = _format_hints(conn)
        material = (f"형식 그래프가 아는 것(되풀이 순): {json.dumps(hints, ensure_ascii=False)}\n\n"
                    f"표본 문서 {len(samples)}건(앞부분):\n{_samples_text(samples)}")

        report(f"LLM이 개념 종류를 제안하는 중(표본 {len(samples)}건)", 2, 4)
        class_proposal = _clean_classes(_ask(db, provider,
            f"사내 문서 저장소의 내용 온톨로지에서 개념 종류를 설계한다. {_CLASS_RULES}\n{_KO}", material).get("classes"))
        report(f"LLM이 개념 종류를 검토하는 중(개념 {len(class_proposal)})", 3, 4)
        class_review = _ask(db, provider,
            "다른 사람이 정한 개념 종류를 검토한다. 표본 문서와 대조해: 겹치는 개념, 표본에 있는데 빠진 개념, 너무 잘게"
            " 나눈 개념, 문서 형식을 개념으로 둔 것을 찾는다. 근거가 표본에 있는 것만 지적한다.\n"
            'JSON: {"verdict":"ok|revise","issues":[{"target":"개념 이름","problem":"","suggestion":""}]}\n' + _KO,
            f"{material}\n\n검토할 개념 종류:\n{json.dumps(class_proposal, ensure_ascii=False)}")
        classes = class_proposal
        if class_review.get("verdict") == "revise" or check_schema({"classes": classes}):
            report(f"검토 의견 {len(class_review.get('issues') or [])}건으로 개념 종류를 고치는 중", 4, 4)
            classes = _clean_classes(_ask(db, provider,
                "검토 의견을 반영해 개념 종류를 고친다. 의견이 틀렸다고 보면 그 항목은 그대로 둔다. 고친 목록만 낸다."
                f" {_CLASS_RULES}\n{_KO}",
                f"{material}\n\n원안:\n{json.dumps(class_proposal, ensure_ascii=False)}\n\n"
                f"검토 의견:\n{json.dumps(class_review, ensure_ascii=False)}").get("classes")) or class_proposal
        names = [c["name"] for c in classes]

        batches = [classes[i:i + RELATION_BATCH] for i in range(0, len(classes), RELATION_BATCH)]
        total = 4 + 2 * len(batches)
        relations: list[dict] = []
        proposed: list[dict] = []
        issues = list(class_review.get("issues") or [])
        seen: set[tuple] = set()

        def keep(found: list[dict], anchors: list[str]) -> list[dict]:
            out = []
            for r in found:
                key = (r["name"], r["from"], r["to"])
                if r["from"] in anchors and r["to"] in names and key not in seen:
                    seen.add(key)
                    out.append(r)
            return out

        for k, batch in enumerate(batches):
            anchors = [c["name"] for c in batch]
            report(f"관계 찾기 {k + 1}/{len(batches)}: {'·'.join(anchors)}", 5 + 2 * k, total)
            # 표본·개념 목록은 묶음마다 같다 — system 앞에 두어 캐시되게 하고, 바뀌는 것(기준 개념·이미 정한 관계)만 뒤에.
            found = _clean_relations(_ask(db, provider,
                f"{material}\n\n개념 목록: {json.dumps(classes, ensure_ascii=False)}\n\n"
                f"아래 **기준 개념에서 나가는** 관계 종류를 찾는다(to는 개념 목록 어디든). {_RELATION_RULES}\n{_KO}",
                f"기준 개념: {json.dumps(anchors, ensure_ascii=False)}\n"
                f"이미 정한 관계: {json.dumps(relations, ensure_ascii=False)}").get("relations"), classes)
            found = [r for r in found if r["from"] in anchors and r["to"] in names]
            proposed += found
            if not found:
                continue
            report(f"관계 검토 {k + 1}/{len(batches)}({len(found)}개)", 6 + 2 * k, total)
            checked = _ask(db, provider,
                f"{material}\n\n개념 목록: {json.dumps(classes, ensure_ascii=False)}\n\n"
                "다른 사람이 찾은 관계 종류를 검토한다. 표본에 근거가 없는 것, 방향이 틀린 것, 이미 정한 관계와 뜻이 같은데"
                " 이름만 다른 것을 찾아 고친다. 고친 **목록 전체**를 relations에 낸다(문제가 없으면 그대로).\n"
                'JSON: {"issues":[{"target":"관계 이름","problem":"","suggestion":""}],'
                ' "relations":[{"name":"","from":"","to":"","description":"","temporal":false}]}\n' + _KO,
                f"이미 정한 관계: {json.dumps(relations, ensure_ascii=False)}\n"
                f"검토할 관계(기준 개념 {anchors}):\n{json.dumps(found, ensure_ascii=False)}")
            issues += [i for i in checked.get("issues") or [] if isinstance(i, dict)]
            reviewed = (_clean_relations(checked["relations"], classes) if isinstance(checked.get("relations"), list)
                        else found)
            relations += keep(reviewed, anchors)

        proposal = {"classes": class_proposal, "relations": proposed}
        revised = {"classes": classes, "relations": relations}
        draft = {"proposal": proposal, "review": {"verdict": "revise" if issues else "ok", "issues": issues},
                 "revised": revised, "problems": check_schema(revised), "batches": len(batches),
                 "samples": [s["path"] for s in samples], "provider": provider.name, "created_at": _now()}
        _put(conn, "schema_draft", draft)
        return draft
    finally:
        conn.close()


def confirm_schema(store_name: str, schema: dict, actor: str) -> dict:
    """관리자가 확정 — 판 번호가 생긴다. 판이 바뀌면 문서 분석은 바뀐 판으로 다시 돈다(바뀐 문서만이 아니라 전부)."""
    schema = _clean_schema(schema)
    problems = check_schema(schema)
    if problems:
        raise SemanticError("스키마를 확정할 수 없습니다: " + " / ".join(problems))
    conn = _conn(store_name)
    try:
        confirmed = {**schema, "version": _digest(schema), "confirmed_at": _now(), "confirmed_by": actor}
        _put(conn, "schema", confirmed)
        _put(conn, "state", {**_get(conn, "state", {}), "paused": None})
        return confirmed
    finally:
        conn.close()


def _schema(conn) -> dict:
    schema = _get(conn, "schema")
    if not schema:
        raise SemanticError("확정한 스키마가 없습니다 — ① 스키마를 먼저 확정하세요.")
    return schema


# --- ②-1 양식 ---

def _rows_of(markdown: str, columns: list[str]) -> list[dict]:
    """문서 안에서 머리글이 columns인 표의 행들 — 마크다운 표와 병합 셀 HTML 표 둘 다."""
    out: list[dict] = []
    # 빈 줄은 건너뛴다 — 표 안에는 빈 줄이 없는데, 캐시(.ready)는 줄마다 빈 줄이 끼어 있을 수 있다(CRLF 본문을
    # 윈도에서 텍스트 모드로 써서 줄 끝이 CR CR LF가 되고, 다시 읽으면 줄이 둘로 갈린다). 그러면 머리글·구분선이 떨어진다.
    lines = [line for line in markdown.splitlines() if line.strip()]
    for i in range(1, len(lines)):
        if ontology._TABLE_SEP_RE.match(lines[i]) and [c for c in ontology._cells(lines[i - 1]) if c] == columns:
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = ontology._cells(lines[j])
                out.append({c: (cells[k] if k < len(cells) else "") for k, c in enumerate(columns)})
                j += 1
    for table in re.findall(r"<table.*?</table>", markdown, re.S | re.I):
        heads = [unescape(re.sub(r"<[^>]+>", "", h)).strip() for h in ontology._HTML_TH_RE.findall(table)]
        if [h for h in heads if h] != columns:
            continue
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I):
            cells = [unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
            if cells:
                out.append({c: (cells[k] if k < len(cells) else "") for k, c in enumerate(columns)})
    return out


def forms(store_name: str) -> list[dict]:
    """양식 목록과 각각의 대응표(제안·검토·확정)."""
    conn = _conn(store_name)
    try:
        saved = _get(conn, "forms", {})
        return [{**f, **saved.get(f["name"], {})} for f in _form_tables(conn)]
    finally:
        conn.close()


def check_mapping(mapping: dict, columns: list[str], schema: dict) -> list[str]:
    classes = {c["name"] for c in schema["classes"]}
    rels = {(r["name"], r["from"], r["to"]) for r in schema["relations"]}
    cols = mapping.get("columns") or {}
    problems = [f"열 '{c}'가 표에 없습니다." for c in cols if c not in columns]
    problems += [f"열 '{c}'의 개념 '{k}'가 스키마에 없습니다." for c, k in cols.items() if k and k not in classes]
    for r in mapping.get("relations") or []:
        a, b = cols.get(r.get("from")), cols.get(r.get("to"))
        if not a or not b or (r.get("rel"), a, b) not in rels:
            problems.append(f"관계 {r.get('from')} —{r.get('rel')}→ {r.get('to')}가 스키마의 관계와 맞지 않습니다.")
    if mapping.get("date") and mapping["date"] not in columns:
        problems.append(f"시점 열 '{mapping['date']}'가 표에 없습니다.")
    return problems


def _form_samples(store_name: str, conn, form: dict) -> list[dict]:
    if form.get("kind") == "list":
        return _list_rows(_list_body(store_name, form["path"]))[1][:FORM_SAMPLE_ROWS * FORM_SAMPLE_DOCS]
    paths = [r["path"] for r in conn.execute(
        "SELECT DISTINCT path FROM nodes WHERE kind = 'table' AND name = ? ORDER BY path LIMIT ?",
        (form["name"], FORM_SAMPLE_DOCS))]
    rows: list[dict] = []
    for path in paths:
        try:
            rows += _rows_of(_markdown(store_name, path), form["columns"])[:FORM_SAMPLE_ROWS]
        except Exception:  # noqa: BLE001
            continue
    return rows


def propose_mapping(db: Session, store_name: str, form_name: str, report=_quiet) -> dict:
    """②-1 양식 하나의 열 대응표 — 제안 → 실제 값 표본과 대조한 검토 → 한 번 고침. 확정은 관리자가."""
    provider = provider_for(db, store_name)
    conn = _conn(store_name)
    try:
        schema = _schema(conn)
        form = next((f for f in _form_tables(conn) if f["name"] == form_name), None)
        if form is None:
            raise SemanticError(f"양식이 없습니다: {form_name}")
        report("실제 값 표본을 모으는 중", 1, 4)
        rows = _form_samples(store_name, conn, form)
        material = (f"스키마: {json.dumps({k: schema[k] for k in ('classes', 'relations')}, ensure_ascii=False)}\n"
                    f"양식 열: {form['columns']} ({form['documents']}개 문서)\n"
                    f"실제 값 표본: {json.dumps(rows[:FORM_SAMPLE_ROWS * FORM_SAMPLE_DOCS], ensure_ascii=False)}")
        rule = ("열마다 그 열의 값이 어떤 개념의 이름인지(값이 개념의 이름이 아니면 null — 금액·날짜·수량은 대개 null),"
                " 같은 행의 두 열 사이에 스키마의 어떤 관계가 있는지, 그 행의 시점이 든 열(date — 작성일·계약일처럼 그 행이"
                " 말하는 때, 없으면 null).\n"
                'JSON: {"columns":{"열 이름":"개념 이름 또는 null"},"relations":[{"from":"열","rel":"관계","to":"열"}],'
                '"date":"열 이름 또는 null"}\n' + _KO)
        report("LLM이 열 대응표를 제안하는 중", 2, 4)
        proposal = _ask(db, provider, "되풀이되는 사내 양식 표의 열을 온톨로지에 대응시킨다. " + rule, material)
        report("LLM이 실제 값 표본과 대조해 검토하는 중", 3, 4)
        review = _ask(db, provider,
            "다른 사람이 만든 양식 열 대응표를 **실제 값 표본과 대조해** 검토한다. 값이 그 개념의 이름이 맞는지"
            " (예: '금액' 열을 협력사로 둔 것, 날짜가 든 열을 품목으로 둔 것), 관계 방향이 맞는지.\n"
            'JSON: {"verdict":"ok|revise","issues":[{"column":"","problem":"","suggestion":""}]}\n' + _KO,
            f"{material}\n\n검토할 대응표:\n{json.dumps(proposal, ensure_ascii=False)}")
        revised = proposal
        if form.get("kind") == "list" and isinstance(proposal, dict) and not proposal.get("date"):
            proposal["date"] = SEEN_DATE     # 데이터 수집 목록 — 그 건이 마지막으로 바뀐 날이 관측 시점이다
        if review.get("verdict") == "revise" or check_mapping(proposal, form["columns"], schema):
            report("검토 의견으로 고치는 중", 4, 4)
            revised = _ask(db, provider, "검토 의견으로 대응표를 고친다. " + rule,
                f"{material}\n\n원안: {json.dumps(proposal, ensure_ascii=False)}\n"
                f"검토 의견: {json.dumps(review, ensure_ascii=False)}\n"
                f"기계 검사: {check_mapping(proposal, form['columns'], schema)}")
        saved = _get(conn, "forms", {})
        saved[form_name] = {**saved.get(form_name, {}), "draft": {
            "proposal": proposal, "review": review, "revised": revised,
            "problems": check_mapping(revised, form["columns"], schema),
            "sample_rows": rows[:FORM_SAMPLE_ROWS], "created_at": _now()}}
        _put(conn, "forms", saved)
        return saved[form_name]
    finally:
        conn.close()


def confirm_mapping(store_name: str, form_name: str, mapping: dict, actor: str) -> dict:
    conn = _conn(store_name)
    try:
        schema = _schema(conn)
        form = next((f for f in _form_tables(conn) if f["name"] == form_name), None)
        if form is None:
            raise SemanticError(f"양식이 없습니다: {form_name}")
        mapping = {"columns": {c: (k or None) for c, k in (mapping.get("columns") or {}).items()},
                   "relations": [r for r in (mapping.get("relations") or []) if isinstance(r, dict)],
                   "date": mapping.get("date") or None}
        problems = check_mapping(mapping, form["columns"], schema)
        if problems:
            raise SemanticError("대응표를 확정할 수 없습니다: " + " / ".join(problems))
        saved = _get(conn, "forms", {})
        saved[form_name] = {**saved.get(form_name, {}), "confirmed": {
            **mapping, "version": _digest(mapping), "schema_version": schema["version"],
            "confirmed_at": _now(), "confirmed_by": actor}}
        _put(conn, "forms", saved)
        return saved[form_name]
    finally:
        conn.close()


def strip_forms(markdown: str, forms: list[list[str]]) -> str:
    """확정한 양식 표(머리글이 forms 중 하나인 표)를 뺀 나머지 글 — 그 표는 규칙으로 뽑으므로 LLM에 다시 보이지 않는다."""
    wanted = [cols for cols in forms]
    lines = markdown.splitlines()
    keep = [True] * len(lines)
    body = [i for i, line in enumerate(lines) if line.strip()]
    for k in range(1, len(body)):
        i = body[k]
        if ontology._TABLE_SEP_RE.match(lines[i]) and [c for c in ontology._cells(lines[body[k - 1]]) if c] in wanted:
            keep[body[k - 1]] = keep[i] = False
            for j in body[k + 1:]:
                if not lines[j].strip().startswith("|"):
                    break
                keep[j] = False
    text = "\n".join(line for line, kept in zip(lines, keep) if kept)

    def html(table: re.Match) -> str:
        heads = [unescape(re.sub(r"<[^>]+>", "", h)).strip() for h in ontology._HTML_TH_RE.findall(table.group(0))]
        return "" if [h for h in heads if h] in wanted else table.group(0)

    return re.sub(r"<table.*?</table>", html, text, flags=re.S | re.I)


def extract_form(markdown: str, columns: list[str], mapping: dict) -> tuple[list[dict], list[dict]]:
    """확정한 대응표로 한 문서의 양식 표에서 개체·관계를 규칙으로 뽑는다(LLM 없음)."""
    return extract_rows(_rows_of(markdown, columns), mapping)


def extract_rows(rows: list[dict], mapping: dict) -> tuple[list[dict], list[dict]]:
    """행들 → 개체·관계(대응표대로). 시점 열(date)이 있으면 그 행의 항목에 관측한 날로 붙인다."""
    cols = mapping["columns"]
    when = mapping.get("date")
    nodes, edges = [], []
    for row in rows:
        evidence = " | ".join(f"{c}: {v}" for c, v in row.items() if v)[:400]
        seen = {"observed_at": iso_date(row.get(when))} if when and iso_date(row.get(when)) else {}
        for col, cls in cols.items():
            value = _space(row.get(col, ""))
            if cls and value:
                nodes.append({"cls": cls, "name": value[:200], "evidence": evidence, **seen})
        for r in mapping.get("relations") or []:
            a, b = _space(row.get(r["from"], "")), _space(row.get(r["to"], ""))
            if a and b:
                edges.append({"src_cls": cols[r["from"]], "src": a[:200], "rel": r["rel"],
                              "dst_cls": cols[r["to"]], "dst": b[:200], "evidence": evidence, **seen})
    return nodes, edges


# --- ②-2 템플릿 ---

def _template_groups(conn) -> dict[str, dict]:
    """템플릿 → {skeleton, paths}. 문서마다 앞부분을 읽어야 해서, 색인이 바뀌지 않았으면 지난 결과를 쓴다."""
    saved = _get(conn, "templates", {})
    row = conn.execute("SELECT COUNT(*) AS n, MAX(indexed_at) AS at FROM docs WHERE body IS NOT NULL").fetchone()
    key = [row["n"], row["at"], sorted(saved)]
    cached = _get(conn, "template_groups")
    if cached and cached.get("key") == key:
        return cached["groups"]
    docs = {r["path"]: st.lines_of(r["head"]) for r in conn.execute(
        "SELECT path, substr(body, 1, ?) AS head FROM docs WHERE body IS NOT NULL", (st.LINE_CHARS,))
        if not _skip_reason(r["path"], r["head"])}
    groups = st.cluster(docs, {tid: t["skeleton"] for tid, t in saved.items()}, TEMPLATE_MIN_DOCS)
    _put(conn, "template_groups", {"key": key, "groups": groups})
    return groups


def templates(store_name: str) -> list[dict]:
    """템플릿 목록 — 뼈대(되풀이되는 줄)·문서 수·규칙(제안·검증·확정)."""
    conn = _conn(store_name)
    try:
        saved = _get(conn, "templates", {})
        out = [{"id": tid, "label": " · ".join(g["skeleton"][:4]), "skeleton": g["skeleton"],
                "documents": len(g["paths"]), "example": (g["paths"] or [""])[0],
                "draft": saved.get(tid, {}).get("draft"), "confirmed": saved.get(tid, {}).get("confirmed"),
                "suspended": saved.get(tid, {}).get("suspended")}
               for tid, g in _template_groups(conn).items()]
        return sorted(out, key=lambda t: (-t["documents"], t["id"]))
    finally:
        conn.close()


_TEMPLATE_RULES = (
    "같은 틀로 되풀이되는 사내 문서에서 개념을 뽑는 **정규식 규칙**을 짠다. 표본 문서들과 그 문서에서 뽑아야 할"
    " 항목(정답)을 준다. 틀이 정한 자리(라벨 다음 줄, 「라벨: 값」 등)에 오는 개념만 규칙으로 만든다 — 문장 속에서"
    " 그때그때 나오는 것은 만들지 않는다(그것은 규칙이 아니라 LLM이 읽는다). pattern은 파이썬 정규식(re.M — ^·$는 줄의"
    " 처음·끝)이고 첫 괄호 묶음이 이름이다. 값이 아니라 **라벨에 기대어** 짠다(특정 회사명·숫자를 넣지 말 것)."
    " 모든 문서에 꼭 있는 필드는 required: true — 그것을 못 찾는 문서는 틀이 다른 것으로 보고 LLM에 넘긴다."
    " relations는 필드 번호(0부터)를 스키마의 관계로 잇는다(from 필드의 첫 값 → to 필드의 값마다).\n"
    'JSON: {"fields":[{"class":"","pattern":"","required":false,"note":""}],"relations":[{"from":0,"rel":"","to":1}]}\n'
    + _KO)
_TEMPLATE_REVISE = (
    "\n\n이번에는 **검증 결과로 규칙을 고친다.** 규칙을 다른 문서들에 돌려 정답과 견준 결과를 준다 — extra는 규칙이"
    " 더 뽑은 것(정답에 없다), missed는 놓친 것, missing_required는 못 찾은 필수 필드다. 정밀도가 재현율보다 중요하다"
    " — 확실하지 않은 필드는 뺀다. 고친 규칙 전체를 같은 JSON으로 낸다.")


def _clean_template_rules(data: dict) -> dict:
    def num(v):
        return int(v) if isinstance(v, int) or (isinstance(v, str) and v.isdigit()) else v
    fields = [{"class": str(f.get("class", "")), "pattern": str(f.get("pattern", "")),
               "required": f.get("required") is True, "note": str(f.get("note", ""))[:200]}
              for f in data.get("fields") or [] if isinstance(f, dict)]
    relations = [{"from": num(r.get("from")), "rel": str(r.get("rel", "")), "to": num(r.get("to"))}
                 for r in data.get("relations") or [] if isinstance(r, dict)]
    return {"fields": fields, "relations": relations}


def _reference(db, provider, conn, schema: dict, store_name: str, path: str) -> dict:
    """정답 — 이 문서에서 LLM이 뽑은 것. 이미 분석했으면 그 결과(LLM 몫만), 아니면 지금 뽑는다(기계 검사까지)."""
    text = _markdown(store_name, path)[:DOC_CHARS]
    row = conn.execute("SELECT status FROM sem_docs WHERE path = ?", (path,)).fetchone()
    if row and row["status"] == "ok":
        nodes = [dict(r) for r in conn.execute(
            "SELECT cls, name, evidence FROM sem_nodes WHERE path = ? AND origin = 'llm'", (path,))]
        edges = [dict(r) for r in conn.execute(
            "SELECT src_cls, src, rel, dst_cls, dst, evidence FROM sem_edges WHERE path = ? AND origin = 'llm'", (path,))]
    else:
        nodes, edges, _dropped, _unanchored = check_extraction(
            _extract_doc(db, provider, schema, path, text), schema, text)
        nodes, edges = [_public(n) for n in nodes], [_public(e) for e in edges]
    return {"path": path, "text": text, "nodes": nodes, "edges": edges}


def _template_samples(refs: list[dict]) -> str:
    return "\n\n".join(
        f"문서 {r['path']}:\n{st.plain(r['text'])[:TEMPLATE_SAMPLE_CHARS]}\n정답: " + json.dumps(
            {"entities": [{"class": n["cls"], "name": n["name"]} for n in r["nodes"]],
             "relations": [{"from": e["src"], "rel": e["rel"], "to": e["dst"]} for e in r["edges"]]},
            ensure_ascii=False) for r in refs)


def _brief(evaluation: dict | None) -> dict | None:
    """고침에 보내는 검증 결과 — 문서마다 틀린 것만."""
    if not evaluation:
        return None
    return {"precision": round(evaluation["precision"], 3), "recall": round(evaluation["recall"], 3),
            "docs": [{k: d[k] for k in ("path", "extra", "missed", "missing_required")} for d in evaluation["docs"]]}


def propose_template(db: Session, store_name: str, tid: str, report=_quiet) -> dict:
    """②-2 템플릿 하나의 추출 규칙 — 정답 만들기(LLM 추출) → 규칙 짜기(LLM) → 다른 문서로 검증 → 모자라면 한 번 고침.
    확정은 관리자가(confirm_template — 검증을 다시 통과해야 한다)."""
    provider = provider_for(db, store_name)
    conn = _conn(store_name)
    try:
        schema = _schema(conn)
        group = _template_groups(conn).get(tid)
        if not group or len(group["paths"]) < TEMPLATE_TRAIN_DOCS + 1:
            raise SemanticError(f"템플릿이 없거나 문서가 {TEMPLATE_TRAIN_DOCS + 1}건보다 적습니다: {tid}")
        paths = group["paths"]
        want = TEMPLATE_TRAIN_DOCS + TEMPLATE_CHECK_DOCS
        picked = paths[::max(1, len(paths) // want)][:want]
        steps = len(picked) + 3
        refs = []
        for i, path in enumerate(picked, 1):
            report(f"정답 만들기 — 문서 {i}/{len(picked)} LLM 추출", i, steps)
            refs.append(_reference(db, provider, conn, schema, store_name, path))
        train, check = refs[:TEMPLATE_TRAIN_DOCS], refs[TEMPLATE_TRAIN_DOCS:]
        system = f"{_TEMPLATE_RULES}\n\n스키마: {_schema_text(schema)}"
        report("규칙 짜기(LLM)", len(picked) + 1, steps)
        proposal = _clean_template_rules(_ask(db, provider, system, _template_samples(train)))
        problems = st.check_rules(proposal, schema)
        first = None if problems else st.evaluate(proposal, check)
        revised, final = proposal, first
        if problems or first["precision"] < TEMPLATE_PRECISION or first["escalated"]:
            report("검증 결과로 고치기(LLM)", len(picked) + 2, steps)
            revised = _clean_template_rules(_ask(
                db, provider, system + _TEMPLATE_REVISE,
                _template_samples(train + check) + "\n\n" + json.dumps(
                    {"규칙": proposal, "기계 검사": problems, "검증": _brief(first)}, ensure_ascii=False)))
            problems = st.check_rules(revised, schema)
            final = None if problems else st.evaluate(revised, check)
        report("검증 끝", steps, steps)
        draft = {"proposal": proposal, "first": _brief(first), "revised": revised, "evaluation": final,
                 "problems": problems, "precision_needed": TEMPLATE_PRECISION,
                 "train": [r["path"] for r in train],
                 "check": [{"path": r["path"], "nodes": r["nodes"], "edges": r["edges"]} for r in check],
                 "created_at": _now()}
        saved = _get(conn, "templates", {})
        saved[tid] = {**saved.get(tid, {}), "skeleton": group["skeleton"], "draft": draft}
        _put(conn, "templates", saved)
        return draft
    finally:
        conn.close()


def confirm_template(store_name: str, tid: str, rules: dict, actor: str) -> dict:
    """관리자가 규칙을 확정 — 고친 규칙이어도 **검증 문서로 다시 재고** 정밀도가 기준을 넘어야 한다(LLM 없이)."""
    rules = _clean_template_rules(rules)
    conn = _conn(store_name)
    try:
        schema = _schema(conn)
        problems = st.check_rules(rules, schema)
        if problems:
            raise SemanticError(" / ".join(problems[:5]))
        saved = _get(conn, "templates", {})
        t = saved.get(tid)
        if not t or not t.get("draft"):
            raise SemanticError("검증할 표본이 없습니다 — 먼저 규칙 제안·검증을 하세요.")
        check = [{**c, "text": _markdown(store_name, c["path"])[:DOC_CHARS]} for c in t["draft"]["check"]]
        ev = st.evaluate(rules, check)
        if ev["precision"] < TEMPLATE_PRECISION:
            raise SemanticError(f"검증 문서에서 정밀도 {ev['precision']:.0%} — 기준 {TEMPLATE_PRECISION:.0%}에 못"
                                " 미칩니다. 규칙을 고치세요.")
        t["confirmed"] = {"rules": rules, "version": st.rules_version(rules), "schema_version": schema["version"],
                          "precision": ev["precision"], "recall": ev["recall"], "confirmed_by": actor,
                          "confirmed_at": _now()}
        t.pop("suspended", None)
        _put(conn, "templates", saved)
        return t["confirmed"]
    finally:
        conn.close()


# --- ③ 문서 ---

# ---------------------------------------------------------------- ②-3 경로 틀 — 폴더·파일 이름의 식별자를 개념으로
#
# 사내 문서 폴더는 폴더 하나가 계약 하나(KR-CT…_차수)·협력사 하나(KR######_회사명)인 경우가 많다(docs/folder-structure.md).
# 경로 틀(services/pathshape)이 찾은 식별자 칸을 관리자가 개념에 대응시키면(「이 칸은 계약」) 그 아래 모든 문서에 규칙으로
# 개체를 단다(origin path — LLM 없음). 경로에서 얻은 사실은 **늘 경로에서 다시 만든다**(apply_paths — 분석이 끝날 때마다):
# 폴더 이름이 바뀌면(차수) 그대로 따라간다. 내용에서 뽑은 것보다 약하다 — 어긋나면 알린다(mismatches).
PATH_MISMATCH_EXAMPLES = 10
_PATH_ID = re.compile(r"식별자 (\S+)")     # _path_nodes가 근거에 떼어 적은 식별자


def _slot_key(slot: dict) -> str:
    return f'{slot["where"]}:{slot.get("parent", "")}:{slot["pattern"]}'


def _path_candidates(store_name: str) -> list[dict]:
    from . import pathshape  # noqa: PLC0415

    prof = pathshape.profile(store_name)
    return [{**s, "key": _slot_key(s)} for s in prof["slots"]
            if s["role"] == "identifier" and not s.get("repeats_folder")]


def path_slots(store_name: str) -> dict:
    """경로 틀 — 식별자 칸 후보마다 **값이 내용 그래프의 어느 개념 개체와 겹치는지**(대응 제안의 근거, LLM 없음), 확정한
    대응, 적용 결과(개체 수·틀 밖 문서), 폴더와 내용이 어긋난 문서."""
    from . import pathshape  # noqa: PLC0415

    candidates = _path_candidates(store_name)
    conn = _conn(store_name)
    try:
        # 스키마가 없어도 후보는 보인다(읽기) — _schema()는 없으면 예외다. 예전에 그것을 불러, 스키마가 없는 저장소는 이
        # 조회가 409가 되고 내용 분석 화면이 통째로 비었다(화면이 상태·양식·템플릿·경로 틀을 한꺼번에 읽는다)
        schema = _get(conn, "schema")
        names: dict[str, set[str]] = {}
        for r in conn.execute("SELECT DISTINCT cls, name FROM sem_nodes WHERE origin != 'path'"):
            names.setdefault(r["cls"], set()).add(_space(r["name"]).lower())
        paths = [r["path"] for r in conn.execute("SELECT path FROM docs")]
        links = _path_links(store_name, candidates, paths)
        analyzed = {r["path"] for r in conn.execute("SELECT path FROM sem_docs WHERE status IN ('ok', 'form', 'template', 'copy')")}
        for c in candidates:
            values, paths_of = {}, {}
            for p in paths:
                for v, tail, _ in pathshape.match(p, c):
                    values.setdefault(v, tail)
                    paths_of.setdefault(v, []).append(p)
            c["sample"] = [{"value": v, "tail": t} for v, t in list(values.items())[:5]]
            c["links"] = links.get(c["key"], [])
            hits = {}
            for cls, known in names.items():
                n = sum(1 for v, t in values.items() if v.lower() in known or (t and _space(t).lower() in known))
                if n:
                    hits[cls] = n
            c["hits"] = dict(sorted(hits.items(), key=lambda kv: -kv[1])[:5])
            c["suggested"] = next(iter(c["hits"]), "")
            # 확신 정도 — 그 값의 문서 중 하나라도 분석이 끝난 값만 견준다(분석하지 않은 폴더는 겹칠 수 없다).
            # contracts는 계약 1,067개 중 분석이 끝난 폴더가 일부라, 전체로 나누면 겹침이 0%에 가깝게 보인다
            if c["suggested"]:
                known = names[c["suggested"]]
                checked = [(v, t) for v, t in values.items() if any(p in analyzed for p in paths_of.get(v, ()))]
                matched = sum(1 for v, t in checked if v.lower() in known or (t and _space(t).lower() in known))
                c["confidence"] = {"cls": c["suggested"], "checked": len(checked), "matched": matched,
                                   "share": round(matched / len(checked), 3) if checked else None}
            c["name_from"] = "tail" if c.get("tail") == "name" else "link" if c["links"] else "value"
        saved = _get(conn, "paths", {})
        confirmed = saved.get("confirmed")
        return {"candidates": candidates, "confirmed": confirmed,
                "stale": bool(confirmed and confirmed.get("schema_version") != (schema or {}).get("version")),
                "applied": _get(conn, "paths_applied"), "mismatches": _path_mismatches(conn, confirmed)}
    finally:
        conn.close()


def confirm_paths(store_name: str, slots: list[dict], actor: str) -> dict:
    """대응을 확정한다 — [{key, cls, name_from}]. 칸은 지금 판정한 후보에서만 고른다(모양·위치는 후보의 것). 빈 목록이면
    대응을 지운다. 확정하면 바로 적용한다(경로에서 만드는 것이라 싸다)."""
    by_key = {c["key"]: c for c in _path_candidates(store_name)}
    conn = _conn(store_name)
    try:
        paths = [r["path"] for r in conn.execute("SELECT path FROM docs")]
        links = _path_links(store_name, list(by_key.values()), paths)
        schema = _schema(conn)
        if not schema:
            raise SemanticError("확정한 스키마가 없습니다 — ① 스키마를 먼저 확정하세요.")
        classes = {c["name"] for c in schema["classes"]}
        chosen, problems = [], []
        for s in slots or []:
            cand = by_key.get(str(s.get("key") or ""))
            if cand is None:
                problems.append(f"지금 경로 틀에 없는 칸입니다: {s.get('key')}")
                continue
            if s.get("cls") not in classes:
                problems.append(f"스키마에 없는 개념입니다: {s.get('cls')}")
                continue
            name_from = "tail" if s.get("name_from") == "tail" and cand.get("tail") == "name" else "value"
            slot = {"key": cand["key"], "where": cand["where"], "parent": cand.get("parent", ""),
                    "pattern": cand["pattern"], "cls": s["cls"], "name_from": name_from}
            if s.get("name_from") == "link":
                link = next((x for x in links.get(cand["key"], []) if x["store"] == s.get("link_store")), None)
                if link is None:
                    problems.append(f"이름을 가져올 저장소가 이 칸과 식별자를 나누지 않습니다: {s.get('link_store')}")
                    continue
                slot.update(name_from="link", link={"store": link["store"], "parent": link["parent"]})
            chosen.append(slot)
        if problems:
            raise SemanticError("경로 틀을 확정할 수 없습니다: " + " / ".join(problems))
        saved = _get(conn, "paths", {})
        saved["confirmed"] = ({"slots": chosen, "version": _digest(chosen), "schema_version": schema["version"],
                               "confirmed_at": _now(), "confirmed_by": actor} if chosen else None)
        _put(conn, "paths", saved)
        applied = apply_paths(conn)
        return {"confirmed": saved["confirmed"], "applied": applied}
    finally:
        conn.close()


def _path_links(store_name: str, candidates: list[dict], paths: list[str]) -> dict[str, list[dict]]:
    """다른 저장소에서 이름을 가져올 수 있는 칸 — {후보 key: [{store, parent, shared, values, names}]}. 같은 모양의 식별자
    폴더에 이름이 붙어 있고(KR{6자리}_{회사명} — credit) 값이 겹치는 저장소. intro는 파일 이름에 코드만 있어 회사명을 거기서
    받는다(실측: intro 147개 중 120개가 credit 폴더의 코드)."""
    from . import pathshape  # noqa: PLC0415

    wanted = {c["pattern"] for c in candidates}
    others = []
    for st in storage_service.indexed_stores():
        if st.name == store_name or not wanted:
            continue
        prof = pathshape.profile(st.name)
        named = [x for x in prof["slots"] if x["where"] == "folder" and x["role"] == "identifier"
                 and x.get("tail") == "name" and x["pattern"] in wanted]
        if named:
            other_paths = pathshape._paths(st.name)
            for x in named:
                others.append((st.name, x, {v for op in other_paths for v, t, _ in pathshape.match(op, x) if t}))
    out: dict[str, list[dict]] = {}
    for c in candidates:
        mine = {v for p in paths for v, _, _ in pathshape.match(p, c)}
        for name, x, vals in others:
            shared = len(mine & vals)
            if x["pattern"] == c["pattern"] and shared:
                out.setdefault(c["key"], []).append({"store": name, "parent": x.get("parent", ""), "shared": shared,
                                                     "values": len(mine), "names": len(vals)})
    return out


def _linked_names(confirmed: dict | None) -> dict[str, dict[str, str]]:
    """「다른 저장소의 이름」으로 확정한 칸마다 {식별자: 이름} — 그 저장소 폴더 이름의 뒤(회사명)."""
    from . import pathshape  # noqa: PLC0415

    out: dict[str, dict[str, str]] = {}
    for slot in (confirmed or {}).get("slots") or []:
        link = slot.get("link")
        if slot["name_from"] != "link" or not link:
            continue
        probe = {"where": "folder", "parent": link.get("parent", ""), "pattern": slot["pattern"]}
        names: dict[str, str] = {}
        for p in pathshape._paths(link["store"]):
            for v, tail, _ in pathshape.match(p, probe):
                if tail:
                    names.setdefault(v, tail)
        out[slot["key"]] = names
    return out


def _path_nodes(confirmed: dict | None, path: str, mtime: float | None,
                linked: dict[str, dict[str, str]] | None = None) -> list[dict]:
    """경로 하나의 개체들. 근거에는 식별자를 **따로 떼어** 적는다(「· 식별자 KR054632」) — 「KR054632_회사명」처럼 밑줄로
    붙어 있으면 묶기의 식별자 패턴(낱말 경계)이 찾지 못한다(밑줄도 낱말 글자다). 그래야 같은 코드의 이름끼리 묶인다(④)."""
    from . import pathshape  # noqa: PLC0415

    out, seen = [], set()
    for slot in (confirmed or {}).get("slots") or []:
        for value, tail, segment in pathshape.match(path, slot):
            if slot["name_from"] == "tail" and tail:
                name = tail
            elif slot["name_from"] == "link":
                name = (linked or {}).get(slot["key"], {}).get(value) or value
            else:
                name = value
            if (slot["cls"], name) in seen:
                continue
            seen.add((slot["cls"], name))
            where = "폴더" if slot["where"] == "folder" else "파일 이름"
            evidence = f"{where} {segment} · 식별자 {value}"
            if slot["name_from"] == "link" and name != value:
                evidence += f" · 이름은 저장소 {slot['link']['store']}의 폴더에서"
            out.append({"cls": slot["cls"], "name": name, "evidence": evidence, "origin": "path",
                        "observed_at": doc_date(path, mtime), "value": value})
    return out


def apply_paths(conn) -> dict:
    """경로에서 얻은 개체를 **전부 다시** 만든다(origin path) — 확정한 대응이 없거나 옛 스키마의 것이면 지우기만. 문서 수만큼
    경로를 맞춰 볼 뿐이라 싸다(실측 contracts 7,615건). 옮기거나 이름을 바꾼 문서도 이것으로 맞는다.

    **먼저 다 계산하고, 쓰기는 한 번에 짧게** 한다. 색인 DB는 rollback 저널이라 쓰기 잠금을 잡은 채 문서를 훑으면 그동안
    다른 요청(화면의 동시 조회·MCP 그래프 질의)과 부딪혀 「database is locked」가 난다 — 지우기를 먼저 해 두고 7천여 건을
    계산하던 예전 순서가 그랬다."""
    confirmed = (_get(conn, "paths", {}) or {}).get("confirmed")
    schema = _get(conn, "schema")     # 없으면 None — 지우기만 한다(_schema()는 없으면 예외)
    usable = bool(confirmed) and confirmed.get("schema_version") == (schema or {}).get("version")
    rows, docs, outside = [], 0, 0
    if usable:
        linked = _linked_names(confirmed)
        for r in list(conn.execute("SELECT path, mtime FROM docs")):
            nodes = _path_nodes(confirmed, r["path"], r["mtime"], linked)
            if nodes:
                docs += 1
                rows += [(r["path"], n["cls"], n["name"], n["evidence"], "path", n["observed_at"]) for n in nodes]
            else:
                outside += 1
    out = ({"nodes": len(rows), "documents": docs, "outside": outside, "version": confirmed["version"], "at": _now()}
           if usable else {"nodes": 0, "documents": 0, "outside": 0, "stale": bool(confirmed)})
    if conn.in_transaction:      # 호출자가 남긴 쓰기가 있으면 먼저 닫는다(BEGIN이 겹치지 않게)
        conn.commit()
    # 바뀐 것이 없으면 쓰지 않는다 — 분석은 예산(120초)마다 이것을 부르는데, 매번 7천여 행을 지우고 다시 쓰면 그동안 화면의
    # 상태 조회가 막혔다(운영 contracts: 「database is locked」). 경로가 바뀐 문서(옮김·이름 바꿈)가 있을 때만 쓴다.
    have = set(map(tuple, conn.execute(
        "SELECT path, cls, name, evidence, origin, observed_at FROM sem_nodes WHERE origin = 'path'")))
    if have == set(rows) and (not usable or (_get(conn, "paths_applied") or {}).get("version") == confirmed["version"]):
        return {**out, **({"at": (_get(conn, "paths_applied") or {}).get("at", out.get("at"))} if usable else {}),
                "unchanged": True}
    try:
        conn.execute("BEGIN IMMEDIATE")      # 쓰기 잠금을 여기서 — 계산은 끝났다
        conn.execute("DELETE FROM sem_nodes WHERE origin = 'path'")
        conn.executemany("INSERT INTO sem_nodes (path, cls, name, evidence, origin, observed_at) VALUES (?, ?, ?, ?, ?, ?)",
                         rows)
        if usable:
            conn.execute("INSERT OR REPLACE INTO sem_meta (key, value) VALUES ('paths_applied', ?)",
                         (json.dumps(out, ensure_ascii=False),))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return out


def _path_mismatches(conn, confirmed: dict | None) -> dict:
    """폴더와 내용이 어긋난 문서 — 경로는 계약 A라는데 내용에서 뽑은 같은 개념·같은 모양의 이름이 A가 아닌 것(다른 계약의
    문서가 잘못 들어갔거나, 문서가 다른 계약을 말한다). 내용을 따르되(경로가 약하다) 사람이 볼 것으로 알린다."""
    out = {"documents": 0, "examples": []}
    slots = [s for s in (confirmed or {}).get("slots") or [] if s["name_from"] == "value"]
    if not slots:
        return out
    by_path: dict[str, dict[str, set[str]]] = {}
    for r in conn.execute("SELECT path, cls, name FROM sem_nodes WHERE origin = 'path'"):
        by_path.setdefault(r["path"], {}).setdefault(r["cls"], set()).add(r["name"])
    seen: set[str] = set()
    for s in slots:
        rx = re.compile(s["pattern"])
        for r in conn.execute("SELECT DISTINCT path, name FROM sem_nodes WHERE cls = ? AND origin != 'path'", (s["cls"],)):
            mine = by_path.get(r["path"], {}).get(s["cls"])
            # 내용의 이름이 「KR-CT230700629_1」처럼 붙어 있어도 앞머리가 그 모양이면 같은 식별자로 본다
            lead = re.split(r"[_\s]", r["name"].strip(), maxsplit=1)[0]
            if mine and rx.fullmatch(lead) and lead not in mine and r["path"] not in seen:
                seen.add(r["path"])
                if len(out["examples"]) < PATH_MISMATCH_EXAMPLES:
                    out["examples"].append({"path": r["path"], "cls": s["cls"], "path_says": sorted(mine)[0],
                                            "content_says": r["name"]})
    out["documents"] = len(seen)
    out["paths"] = sorted(seen)[:500]
    return out


def _path_hint(confirmed: dict | None, path: str, linked: dict | None = None) -> str:
    """LLM 추출에 함께 주는 한 줄 — 이 문서가 경로로 속한 개체. 관계의 한쪽 끝을 문서 안에서 짐작하지 않게."""
    nodes = _path_nodes(confirmed, path, None, linked)
    if not nodes:
        return ""
    return ("경로가 말하는 것(관리자가 확정한 경로 틀 — 이 문서가 속한 개체): "
            + ", ".join(f'{n["cls"]} {n["name"]}' for n in nodes) + "\n")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def check_extraction(data: dict, schema: dict, text: str) -> tuple[list[dict], list[dict], int, int]:
    """기계 검사 — 전부에 건다. (통과한 개체, 관계, 버린 수, 이름이 근거에 없는 수)

    **버린다:** 스키마에 없는 개념·관계, 근거 문장이 원문에 없는 것(지어낸 근거). 대조는 공백을 빼고 한다 — PDF는
    낱말 가운데에서도 줄을 바꿔(`다\n만`) 글자 그대로 옮긴 문장이 공백 하나 때문에 원문에 없다고 나온다(실측).

    **남기고 검토로 보낸다:** 근거는 원문에 있는데 이름이 그 안에 글자 그대로 없는 것. 재무위험·업무절차처럼 이름이
    문장의 요약인 개념에서 흔하고(실측: 신용평가서의 「매출 외형 감소」), 버리면 그 개념의 관계가 통째로 빠진다.
    이런 항목이 있으면 그 문서는 표본이 아니어도 LLM 검토를 받는다(analyze)."""
    classes = {c["name"] for c in schema["classes"]}
    rels = {(r["name"], r["from"], r["to"]) for r in schema["relations"]}
    plain = _compact(text)
    dropped = unanchored = 0
    nodes, edges = [], []

    def check(evidence: str, *names: str) -> bool | None:
        """근거가 원문에 없으면 None(버린다), 있으면 이름이 근거 안에 다 있는가."""
        ev = _compact(evidence)
        if not ev or ev not in plain:
            return None
        return all(_compact(n) and _compact(n) in ev for n in names)

    for e in data.get("entities") or []:
        if not isinstance(e, dict):
            dropped += 1
            continue
        cls, name, ev = str(e.get("class", "")), str(e.get("name", "")), str(e.get("evidence", ""))
        anchored = check(ev, name) if cls in classes and name.strip() else None
        if anchored is None:
            dropped += 1
            continue
        unanchored += not anchored
        nodes.append({"cls": cls, "name": _space(name)[:200], "evidence": _space(ev)[:400], "anchored": anchored})
    for r in data.get("relations") or []:
        if not isinstance(r, dict) or not isinstance(r.get("from"), dict) or not isinstance(r.get("to"), dict):
            dropped += 1
            continue
        a, b = r["from"], r["to"]
        key = (str(r.get("rel", "")), str(a.get("class", "")), str(b.get("class", "")))
        ev = str(r.get("evidence", ""))
        names = (str(a.get("name", "")), str(b.get("name", "")))
        anchored = check(ev, *names) if key in rels and all(n.strip() for n in names) else None
        if anchored is None:
            dropped += 1
            continue
        unanchored += not anchored
        start, end = iso_date(r.get("valid_from")), iso_date(r.get("valid_to"))
        bad_range = bool(start and end and start > end)
        if bad_range:      # 기간이 거꾸로다 — 관계는 남기고 기간만 버린다(문서가 말한 것은 관계다)
            start = end = ""
        edges.append({"src_cls": key[1], "src": _space(names[0])[:200], "rel": key[0],
                      "dst_cls": key[2], "dst": _space(names[1])[:200], "evidence": _space(ev)[:400],
                      "valid_from": start, "valid_to": end, "anchored": anchored, "bad_range": bad_range})
    return nodes, edges, dropped, unanchored


def _sampled(path: str) -> bool:
    return int(hashlib.sha1(path.encode()).hexdigest(), 16) % REVIEW_EVERY == 0


def _schema_text(schema: dict) -> str:
    return json.dumps({k: schema[k] for k in ("classes", "relations")}, ensure_ascii=False)


# 지시문과 스키마는 문서마다 같다 — **system에 함께 두고 문서는 뒤(user)에만** 둔다. 그러면 앞부분이 매번 같아
# 프롬프트 캐시가 걸린다(Bedrock은 system 끝의 캐시 지점, OpenAI 계열은 같은 앞부분을 저절로 캐시한다). 예전에는
# 스키마를 문서와 함께 user에 넣어 6천 건이면 같은 스키마를 6천 번 새로 읽혔다.
_EXTRACT_RULES = (
    "사내 문서의 **내용**에서 개념과 관계를 뽑는다. 확정한 스키마의 개념·관계 종류만 쓴다. evidence는 문서의"
    " 문장을 **글자 그대로** 옮긴다(요약·번역 금지) — 되도록 개체 이름이 들어 있는 문장으로. outside에는 스키마에"
    " 없는 **개념 종류**(예: 인증·공장)만 적는다 — 개별 회사명·등급 값 같은 개체는 적지 않는다.\n"
    " 시점에 매인 관계(스키마의 temporal)는 문서에 기간·날짜가 적혀 있으면 valid_from·valid_to(YYYY-MM-DD)로"
    " 옮긴다 — 적혀 있지 않으면 비운다(지어내지 않는다)."
    # 운영 표본 검토에서 틀린 항목의 모양(2026-11, contracts) — 규칙으로 막는다
    " 관계의 evidence에는 **양쪽 개체의 이름이 모두** 들어 있어야 한다 — 한쪽 이름이 다른 문장에만 있으면 그 관계는 내지"
    " 않는다(문서 전체의 문맥으로 잇지 않는다). 개체 이름에 근거 문장에 없는 값(금액·날짜·비율)을 덧붙이지 않는다."
    " 기준 지식(layer reference) 개념 사이의 관계는 문서가 그것을 **규칙으로** 말할 때만(규정·표준 서식·지침) 낸다 —"
    " 계약서 한 건의 조항 제목만으로 「이 계약 유형은 이 조항을 포함한다」 같은 일반 관계를 만들지 않는다.\n"
    'JSON: {"entities":[{"class":"","name":"","evidence":""}],'
    ' "relations":[{"from":{"class":"","name":""},"rel":"","to":{"class":"","name":""},"evidence":"",'
    '"valid_from":"","valid_to":""}],'
    ' "outside":[{"name":"","note":""}]}\n' + _KO)
_REVIEW_RULES = (
    "다른 사람이 문서에서 뽑은 개념·관계를 검토한다. 항목마다 **그 근거 문장이** 그것을 실제로 말하는지, 개념 종류가"
    " 맞는지 본다(근거 문장만 보고 판단한다). 틀린 항목의 i만 적는다.\n"
    'JSON: {"wrong":[{"i":0,"problem":""}]}\n' + _KO)


def _extract_doc(db, provider, schema: dict, path: str, text: str, hint: str = "") -> dict:
    return _ask(db, provider, f"{_EXTRACT_RULES}\n\n스키마: {_schema_text(schema)}", f"문서 {path}:\n{hint}{text}")


def _review_items(db, provider, schema: dict, path: str, items: list[dict]) -> dict[int, str]:
    """검토 — 문서 전체가 아니라 **항목과 근거 문장만** 보낸다. 근거가 원문에 있는지는 기계 검사가 이미 봤고, 남은
    질문은 "이 문장이 이것을 말하는가"라 문장으로 답할 수 있다(문서를 다시 보내면 검토 한 번이 추출만큼 든다).
    items는 [{"entity"|"relation": …}] — 돌려주는 번호는 items의 자리다."""
    if not items:
        return {}
    numbered = [{"i": i, **item} for i, item in enumerate(items)]
    out = _ask(db, provider, f"{_REVIEW_RULES}\n\n스키마: {_schema_text(schema)}",
               f"문서 {path}의 항목:\n{json.dumps(numbered, ensure_ascii=False)}")
    return {int(w["i"]): str(w.get("problem", ""))[:200] for w in out.get("wrong") or []
            if isinstance(w, dict) and str(w.get("i", "")).isdigit() and int(w["i"]) < len(items)}


def _tally_review(state: dict, path: str, items: list[dict], wrong: dict[int, str], sampled: bool) -> None:
    """검토 결과를 센다. 오류율은 **무작위 표본만**으로 잰다 — 이름이 근거에 없는 항목은 원래 틀리기 쉬워, 섞으면
    멈춤 기준을 괜히 넘는다. 그쪽은 따로 센다. 무엇을 왜 틀렸다고 했는지 남긴다 — 멈췄을 때 관리자가 스키마를
    고칠지, 그냥 다시 시작할지 고를 근거다."""
    bucket = "review" if sampled else "check"
    state[f"{bucket}_items"] = state.get(f"{bucket}_items", 0) + len(items)
    state[f"{bucket}_wrong"] = state.get(f"{bucket}_wrong", 0) + len(wrong)
    notes = state.get("review_notes", [])
    for j, problem in wrong.items():
        item = items[j].get("entity") or items[j].get("relation")
        label = f'{item["cls"]}:{item["name"]}' if "cls" in item else f'{item["src"]} —{item["rel"]}→ {item["dst"]}'
        notes.append({"path": path, "item": label, "evidence": item["evidence"][:160], "problem": problem,
                      "sampled": sampled})
    state["review_notes"] = notes[-30:]


def _public(item: dict) -> dict:
    """저장·검토에 보내는 모양 — 기계 검사가 붙인 표시(anchored·bad_range)는 뺀다."""
    return {k: v for k, v in item.items() if k not in ("anchored", "bad_range")}


def _skip_reason(path: str, body: str) -> str:
    from . import doctext  # noqa: PLC0415

    ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path.rsplit("/", 1)[-1] else ""
    if ext in SKIP_EXTENSIONS:
        return f"분석 대상이 아닌 형식({ext})"
    broken = doctext.garbled_ratio(body)
    if broken >= doctext.GARBLED:
        # 코드 페이지가 맞지 않게 뽑힌 글(한글이 ?로) — LLM은 그 사이의 숫자로 이름을 지어낸다(실측 표본 오류의 대부분)
        return f"본문 글자가 깨져 있습니다(깨진 글자 {broken:.0%}) — 추출을 고친 뒤 다시 추출하세요(ontology_reextract)"
    if len(_compact(body)) < MIN_CHARS:
        return f"읽을 글이 거의 없습니다({len(_compact(body))}자)"
    return ""


def _record(conn, path: str, fp: str, status: str, *, error: str | None = None, truncated: int = 0,
            dropped: int = 0, reviewed: int = 0) -> None:
    conn.execute("INSERT OR REPLACE INTO sem_docs (path, fp, status, error, truncated, dropped, reviewed, analyzed_at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (path, fp, status, error, truncated, dropped, reviewed, time.time()))


def _copy(conn, source: str, target: str, observed: str = "") -> None:
    """본문이 같은 문서의 결과를 옮긴다(LLM 없이). 근거 문장도 같은 본문의 것이라 그대로 맞다. 관측한 날은 옮긴
    문서의 날이다(같은 본문을 다른 날 다시 받았다 — 그날에도 그렇다는 관측이다). 행마다 날이 있던 것(목록)은 그대로.
    경로에서 얻은 것(origin path)은 옮기지 않는다 — 경로마다 다르다(apply_paths가 경로에서 다시 만든다)."""
    conn.execute("DELETE FROM sem_nodes WHERE path = ? AND origin != 'path'", (target,))
    conn.execute("DELETE FROM sem_edges WHERE path = ?", (target,))
    conn.execute("INSERT INTO sem_nodes (path, cls, name, evidence, origin, observed_at, anchored)"
                 " SELECT ?, cls, name, evidence, origin, CASE WHEN origin = 'list' THEN observed_at ELSE ? END, anchored"
                 " FROM sem_nodes WHERE path = ? AND origin != 'path'", (target, observed, source))
    conn.execute("INSERT INTO sem_edges (path, src_cls, src, rel, dst_cls, dst, evidence, origin, observed_at,"
                 " valid_from, valid_to, anchored) SELECT ?, src_cls, src, rel, dst_cls, dst, evidence, origin,"
                 " CASE WHEN origin = 'list' THEN observed_at ELSE ? END, valid_from, valid_to, anchored"
                 " FROM sem_edges WHERE path = ?", (target, observed, source))


def _write(conn, path: str, nodes: list[dict], edges: list[dict], origin: str, observed: str = "") -> None:
    """한 문서의 내용 그래프를 갈아 끼운다. 항목에 origin이 있으면 그것(양식 규칙과 LLM이 한 문서에 섞인다).
    관측한 날(observed)은 그 문서의 날 — 항목에 제 날이 있으면(목록의 행) 그것. 경로에서 얻은 것(origin path)은 그대로 둔다 —
    본문이 아니라 경로에서 나왔다(apply_paths)."""
    conn.execute("DELETE FROM sem_nodes WHERE path = ? AND origin != 'path'", (path,))
    conn.execute("DELETE FROM sem_edges WHERE path = ?", (path,))
    conn.executemany("INSERT INTO sem_nodes (path, cls, name, evidence, origin, observed_at, anchored)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [(path, n["cls"], n["name"], n["evidence"], n.get("origin", origin),
                       n.get("observed_at") or observed, _anchored(n)) for n in nodes])
    conn.executemany(
        "INSERT INTO sem_edges (path, src_cls, src, rel, dst_cls, dst, evidence, origin, observed_at, valid_from,"
        " valid_to, anchored) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(path, e["src_cls"], e["src"], e["rel"], e["dst_cls"], e["dst"], e["evidence"], e.get("origin", origin),
          e.get("observed_at") or observed, e.get("valid_from") or "", e.get("valid_to") or "", _anchored(e))
         for e in edges])


def analyze(db: Session, store_name: str, budget_seconds: float = 120.0, should_stop=lambda: False) -> dict:
    """③ 바뀐 문서만 분석한다(문서 내용·스키마 판·대응표 판이 같으면 다시 부르지 않는다). 예산 안에서 돌고
    남은 수를 돌려준다. 양식 문서는 확정한 대응표로 규칙 추출(LLM 없음), 나머지는 LLM 추출 + 기계 검사 +
    표본 검토. 표본 오류율이 기준을 넘으면 멈춘다(paused)."""
    started = time.monotonic()
    conn = _conn(store_name)
    try:
        schema = _schema(conn)
        state = _get(conn, "state", {})
        if state.get("paused"):
            return {"done": True, "paused": state["paused"], "analyzed": 0, "remaining": 0}
        provider = provider_for(db, store_name)
        root = _root(store_name)
        confirmed_forms = {name: f["confirmed"] for name, f in _get(conn, "forms", {}).items()
                           if f.get("confirmed") and f["confirmed"].get("schema_version") == schema["version"]}
        saved_templates = _get(conn, "templates", {})
        template_rules = {tid: t["confirmed"] for tid, t in saved_templates.items()
                          if t.get("confirmed") and t["confirmed"].get("schema_version") == schema["version"]}
        skeletons = {tid: saved_templates[tid]["skeleton"] for tid in template_rules}
        path_conf = (_get(conn, "paths", {}) or {}).get("confirmed")
        if path_conf and path_conf.get("schema_version") != schema["version"]:
            path_conf = None        # 옛 스키마의 경로 틀 — 쓰지 않는다(apply_paths도 지운다)
        path_linked = _linked_names(path_conf)
        form_of: dict[str, list[str]] = {}
        for r in conn.execute("SELECT DISTINCT path, name FROM nodes WHERE kind = 'table'"):
            if r["name"] in confirmed_forms:
                form_of.setdefault(r["path"], []).append(r["name"])
        lists = {d["path"] for d in _list_docs(conn)}
        # H. 층위별 검토 — 기준 지식(규정·용어·절차)은 한 번 정해 많은 답에 퍼진다: 그 항목은 표본이 아니어도 검토한다
        layers = layer_of(schema)
        reference = {c for c, v in layers.items() if v == "reference"}
        rows = list(conn.execute("SELECT path, fp, status FROM sem_docs"))
        known = {r["path"]: r["fp"] for r in rows}
        # ② 같은 본문은 한 번만 — 경로만 다르고 본문·스키마 판·대응표 판이 같으면(지문이 같으면) 앞의 결과를 옮긴다.
        done_fp = {r["fp"]: r["path"] for r in rows if r["status"] in ("ok", "form", "template")}
        paths = _paths(conn)
        # 지워진 문서의 내용 그래프는 **이번 분석이 끝난 뒤에** 지운다(아래). 탐색기로 옮기거나 폴더 이름을 바꾸면 색인에는
        # 「옛 경로가 없어지고 새 경로가 생긴」 것으로 오고, 새 경로는 본문이 같아 옛 경로의 결과를 옮겨 받는다(②) — 먼저
        # 지우면 빈 결과를 옮겨 받고, 지문이 같아 다시 분석되지도 않는다(실측: 계약 폴더의 차수 이름 바꾸기).
        gone = set(known) - set(paths)

        stats = {"analyzed": 0, "form_docs": 0, "template_docs": 0, "escalated": 0, "failed": 0, "reviewed": 0,
                 "dropped": 0, "copied": 0, "skipped": 0}
        outside: dict = _get(conn, "outside", {})
        remaining = 0
        concurrency = max(1, int(get_settings().semantic_concurrency))
        jobs: list[dict] = []
        queued_fp: dict[str, str] = {}
        deferred: list[tuple] = []      # (원본, 사본, 지문, 관측한 날) — 원본의 LLM 결과가 나오면 옮긴다
        max_jobs = concurrency * 8      # 한 번(예산 120초)에 끝낼 만큼만 모은다 — 남은 문서는 다음 번에
        full = False
        for path in paths:
            fms = sorted(form_of.get(path, []))
            doc = conn.execute("SELECT body, mtime FROM docs WHERE path = ?", (path,)).fetchone()
            body = doc["body"] or ""
            observed = doc_date(path, doc["mtime"])
            listed = LIST_PREFIX + path if path in lists else ""
            if listed in confirmed_forms:
                fms = [listed]
            tid = st.member_of(st.lines_of(body), skeletons) if skeletons else None
            parts = [hashlib.sha1(body.encode()).hexdigest(), schema["version"], [confirmed_forms[f]["version"] for f in fms]]
            fp = _digest(parts + [template_rules[tid]["version"]] if tid else parts)   # 템플릿 밖 문서의 지문은 그대로
            if known.get(path) == fp:
                continue
            # 같은 본문을 옮겨 받는 것(LLM 없음 — 싸다)은 예산을 넘겨도 한다: 옮길 원본(지워진 옛 경로)은 이번 분석이 끝나면 지운다
            cheap = fp in queued_fp or done_fp.get(fp) not in (None, path)
            if not cheap and (full or time.monotonic() - started > budget_seconds or should_stop()):
                remaining += 1
                continue
            try:
                if fp in queued_fp and queued_fp[fp] != path:   # 같은 본문이 이번에 LLM으로 가 있다 — 끝나면 옮긴다
                    deferred.append((queued_fp[fp], path, fp, observed))
                    continue
                twin = done_fp.get(fp)
                if twin and twin != path:
                    _copy(conn, twin, path, observed)
                    _record(conn, path, fp, "copy")
                    stats["copied"] += 1
                    stats["analyzed"] += 1
                    conn.commit()
                    continue
                # D. 데이터 수집 목록 — 행마다 건이고 「수집 바뀜」이 그 건의 관측 시점. 대응표로만(LLM 없음)
                if listed:
                    if listed in confirmed_forms:
                        nodes, edges = extract_rows(_list_rows(_list_body(store_name, path, root))[1],
                                                    confirmed_forms[listed])
                        _write(conn, path, [{**n, "origin": "list"} for n in nodes],
                               [{**e, "origin": "list"} for e in edges], "list", observed)
                        _record(conn, path, fp, "form")
                        stats["form_docs"] += 1
                    else:
                        _write(conn, path, [], [], "list")
                        _record(conn, path, fp, "skipped",
                                error="데이터 수집 목록 — ②-1 양식에서 열 대응을 확정하면 행마다 규칙으로 뽑습니다")
                        stats["skipped"] += 1
                    stats["analyzed"] += 1
                    done_fp.setdefault(fp, path)
                    conn.commit()
                    continue
                # ③ 분석할 것이 없는 문서 — 작업 파일(확장자)이나 읽을 글이 거의 없는 것
                reason = _skip_reason(path, body)
                if reason:
                    _write(conn, path, [], [], "llm")
                    _record(conn, path, fp, "skipped", error=reason)
                    stats["skipped"] += 1
                    stats["analyzed"] += 1
                    conn.commit()
                    continue
                markdown = _markdown(store_name, path, root)
                form_nodes, form_edges = [], []
                for f in fms:
                    n, e = extract_form(markdown, f.split(" | "), confirmed_forms[f])
                    form_nodes += [{**x, "origin": "form"} for x in n]
                    form_edges += [{**x, "origin": "form"} for x in e]
                rest = strip_forms(markdown, [f.split(" | ") for f in fms]) if fms else markdown
                if fms and len(_compact(rest)) < FORM_REST_CHARS:
                    # 양식 문서(견적서) — 표 밖에 읽을 글이 거의 없다. 규칙으로만.
                    _write(conn, path, form_nodes, form_edges, "form", observed)
                    status, dropped, reviewed = "form", 0, 0
                    stats["form_docs"] += 1
                elif tid and not (by_rule := st.apply_rules(template_rules[tid]["rules"], rest[:DOC_CHARS]))[2] \
                        and (by_rule[0] or by_rule[1]):
                    # 템플릿 문서 — 확정한 규칙으로(LLM 추출 없음). 표본이면 규칙이 뽑은 것도 LLM이 검토한다 —
                    # 오류율(멈춤 조건)에 함께 센다. 필수 필드를 못 찾으면 이 분기로 오지 않는다(LLM으로 넘긴다).
                    nodes, edges = by_rule[0], by_rule[1]
                    status, dropped, reviewed = "template", 0, 0
                    if _sampled(path):
                        items = [{"entity": n} for n in nodes] + [{"relation": e} for e in edges]
                        wrong = _review_items(db, provider, schema, path, items)
                        _tally_review(state, path, items, wrong, True)
                        nodes = [n for i, n in enumerate(nodes) if i not in wrong]
                        edges = [e for i, e in enumerate(edges) if len(by_rule[0]) + i not in wrong]
                        reviewed = 1
                        stats["reviewed"] += 1
                    _write(conn, path, form_nodes + nodes, form_edges + edges, "template", observed)
                    stats["template_docs"] += 1
                else:
                    # LLM으로 읽는 문서 — 지금 부르지 않고 모았다가 아래에서 동시에(semantic_concurrency) 부른다
                    stats["escalated"] += bool(tid)      # 템플릿인데 필수 필드가 없다 — 틀이 다르다
                    jobs.append({"path": path, "fp": fp, "text": rest[:DOC_CHARS], "long": len(rest) > DOC_CHARS,
                                 "form_nodes": form_nodes, "form_edges": form_edges, "observed": observed,
                                 "hint": _path_hint(path_conf, path, path_linked)})
                    queued_fp[fp] = path
                    if len(jobs) >= max_jobs:
                        full = True
                    continue
                _record(conn, path, fp, status, truncated=int(status == "ok" and len(rest) > DOC_CHARS),
                        dropped=dropped, reviewed=reviewed)
                done_fp.setdefault(fp, path)
                stats["analyzed"] += 1
            except Exception as e:  # noqa: BLE001 — 한 문서의 실패가 나머지를 막지 않게, 사유를 남긴다
                conn.execute("INSERT OR REPLACE INTO sem_docs (path, fp, status, error, analyzed_at)"
                             " VALUES (?, ?, 'failed', ?, ?)", (path, fp, f"{type(e).__name__}: {e}"[:300], time.time()))
                stats["failed"] += 1
            conn.commit()
            items = state.get("review_items", 0)
            if items >= STOP_MIN_ITEMS and state.get("review_wrong", 0) / items > STOP_ERROR_RATE:
                state["paused"] = (f"표본 검토에서 {state['review_wrong']}/{items}개가 틀렸습니다"
                                   f"(기준 {int(STOP_ERROR_RATE * 100)}%) — 스키마나 대응표를 고친 뒤 다시 시작하세요.")
                _put(conn, "state", state)
                break
            _put(conn, "state", state)
        conn.commit()       # LLM을 기다리는 동안 색인 파일을 잡고 있지 않게
        if not state.get("paused"):
            remaining += _run_llm_jobs(conn, store_name, schema, reference, jobs, state, stats, outside, done_fp,
                                       concurrency, started, budget_seconds, should_stop)
        for source, target, fp, observed in deferred:   # ② 같은 본문 — 원본이 끝났으면 옮기고, 아니면 다음 번에
            if done_fp.get(fp) == source:
                _copy(conn, source, target, observed)
                _record(conn, target, fp, "copy")
                stats["copied"] += 1
                stats["analyzed"] += 1
            else:
                remaining += 1
        for path in gone:      # 지워진 문서의 내용 그래프 — 옮겨 받을 것은 위에서 다 옮겼다
            _write(conn, path, [], [], "llm")
            conn.execute("DELETE FROM sem_docs WHERE path = ?", (path,))
        conn.commit()
        stats["path"] = apply_paths(conn)    # 경로에서 얻은 개체 — 옮기거나 이름을 바꾼 문서까지 경로에서 다시
        outside = dict(sorted(outside.items(), key=lambda kv: -kv[1])[:100])
        _put(conn, "outside", outside)
        return {**stats, "remaining": remaining, "done": remaining == 0 and not state.get("paused"),
                "paused": state.get("paused")}
    finally:
        conn.close()


def _llm_doc(store_name: str, schema: dict, reference: set[str], job: dict) -> dict:
    """LLM으로 읽는 문서 하나 — 추출 → 기계 검사 → 검토할 것만 검토. 작업 스레드에서 돈다(자기 세션 — 색인 파일은 만지지
    않는다). ① 검토할 것만 고른다: 표본(10%)이면 전부 — 이것이 오류율(멈춤 조건)의 표본이다. 표본이 아니면 이름이 근거에
    없는 항목과 기준 지식(H)만. 버린 것이 더 많으면 전부."""
    from ..db import SessionLocal  # noqa: PLC0415

    path, text = job["path"], job["text"]
    with SessionLocal() as wdb:
        provider = provider_for(wdb, store_name)
        data = _extract_doc(wdb, provider, schema, path, text, job.get("hint", ""))
        nodes, edges, dropped, _unanchored = check_extraction(data, schema, text)
        sampled = _sampled(path)
        every = sampled or (dropped and dropped >= (len(nodes) + len(edges)))
        targets = [("n", i) for i, n in enumerate(nodes) if every or not n["anchored"] or n["cls"] in reference] + \
                  [("e", i) for i, e in enumerate(edges)
                   if every or not e["anchored"] or {e["src_cls"], e["dst_cls"]} & reference]
        items = [{"entity": _public(nodes[i])} if k == "n" else {"relation": _public(edges[i])} for k, i in targets]
        wrong = _review_items(wdb, provider, schema, path, items) if items else {}
    return {"data": data, "nodes": nodes, "edges": edges, "dropped": dropped, "sampled": sampled, "targets": targets,
            "items": items, "wrong": wrong}


def _run_llm_jobs(conn, store_name: str, schema: dict, reference: set[str], jobs: list[dict], state: dict, stats: dict,
                  outside: dict, done_fp: dict, concurrency: int, started: float, budget: float, should_stop) -> int:
    """모은 LLM 문서를 동시에 — 결과는 이 스레드가 받아 쓴다(색인 파일에 쓰는 것은 하나뿐이다). 예산을 넘기거나 멈추라고
    하면 새로 보내지 않는다(돌던 것은 끝까지 받아 쓴다). 멈춤 기준을 넘으면 그 자리에서 멈춘다. 보내지 못한 수를 돌려준다."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait  # noqa: PLC0415

    queue = list(jobs)
    flying: dict = {}
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix=f"semantic-{store_name}") as pool:
        while queue or flying:
            while queue and len(flying) < concurrency and not state.get("paused") \
                    and time.monotonic() - started <= budget and not should_stop():
                job = queue.pop(0)
                flying[pool.submit(_llm_doc, store_name, schema, reference, job)] = job
            if not flying:
                break
            finished, _ = wait(list(flying), return_when=FIRST_COMPLETED)
            for fut in finished:
                job = flying.pop(fut)
                path, fp = job["path"], job["fp"]
                try:
                    r = fut.result()
                    nodes, edges, targets, wrong = r["nodes"], r["edges"], r["targets"], r["wrong"]
                    stats["range_errors"] = stats.get("range_errors", 0) + sum(e["bad_range"] for e in edges)
                    reviewed = 0
                    if targets:
                        reviewed = 1
                        _tally_review(state, path, r["items"], wrong, r["sampled"])
                        drop = {targets[j] for j in wrong}
                        nodes = [n for i, n in enumerate(nodes) if ("n", i) not in drop]
                        edges = [e for i, e in enumerate(edges) if ("e", i) not in drop]
                        stats["reviewed"] += 1
                    for o in r["data"].get("outside") or []:
                        if isinstance(o, dict) and o.get("name"):
                            name = str(o["name"])[:60]
                            outside[name] = outside.get(name, 0) + 1
                    _write(conn, path, job["form_nodes"] + [{**_public(n), "anchored": n["anchored"]} for n in nodes],
                           job["form_edges"] + [{**_public(e), "anchored": e["anchored"]} for e in edges], "llm",
                           job["observed"])
                    _record(conn, path, fp, "ok", truncated=int(job["long"]), dropped=r["dropped"], reviewed=reviewed)
                    stats["dropped"] += r["dropped"]
                    done_fp.setdefault(fp, path)
                    stats["analyzed"] += 1
                except Exception as e:  # noqa: BLE001 — 한 문서의 실패가 나머지를 막지 않게, 사유를 남긴다
                    conn.execute("INSERT OR REPLACE INTO sem_docs (path, fp, status, error, analyzed_at)"
                                 " VALUES (?, ?, 'failed', ?, ?)", (path, fp, f"{type(e).__name__}: {e}"[:300], time.time()))
                    stats["failed"] += 1
                items = state.get("review_items", 0)
                if items >= STOP_MIN_ITEMS and state.get("review_wrong", 0) / items > STOP_ERROR_RATE \
                        and not state.get("paused"):
                    state["paused"] = (f"표본 검토에서 {state['review_wrong']}/{items}개가 틀렸습니다"
                                       f"(기준 {int(STOP_ERROR_RATE * 100)}%) — 스키마나 대응표를 고친 뒤 다시 시작하세요.")
                _put(conn, "state", state)
                conn.commit()
    return len(queue)


def resume(store_name: str) -> None:
    """멈춘 분석을 다시 — 표본 오류 집계도 비운다(고친 스키마·대응표로 새로 잰다)."""
    conn = _conn(store_name)
    try:
        _put(conn, "state", {"paused": None, "review_items": 0, "review_wrong": 0, "review_notes": [],
                             "check_items": 0, "check_wrong": 0})
    finally:
        conn.close()


# 분석은 오래 걸린다(문서마다 LLM 한 번, 최대 수 분) — 스케줄러 스레드를 붙잡지 않게 따로 돈다. 저장소마다 하나.
_workers: dict[str, threading.Thread] = {}
_stop: set[str] = set()
_lock = threading.Lock()


def running(store_name: str) -> bool:
    worker = _workers.get(store_name)
    return worker is not None and worker.is_alive()


def start(store_name: str) -> bool:
    """뒤에서 끝까지 돈다(멈춤 조건이나 stop까지). 이미 돌고 있으면 아무것도 하지 않는다."""
    from ..db import SessionLocal  # noqa: PLC0415

    with _lock:
        if running(store_name):
            return False
        _stop.discard(store_name)

        def loop():
            while store_name not in _stop:
                db = SessionLocal()
                try:
                    result = analyze(db, store_name, should_stop=lambda: store_name in _stop)
                except Exception as e:  # noqa: BLE001
                    result = {"done": True, "error": str(e)[:300]}
                finally:
                    db.close()
                conn = _conn(store_name)
                try:
                    _put(conn, "last_run", {**result, "at": _now()})
                finally:
                    conn.close()
                if result.get("done") or result.get("paused") or result.get("error"):
                    break

        worker = threading.Thread(target=loop, daemon=True, name=f"semantic-{store_name}")
        _workers[store_name] = worker
        worker.start()
        return True


def stop(store_name: str) -> None:
    _stop.add(store_name)


# 제안(①·②·④)도 LLM을 여러 번 부른다 — 요청 안에서 기다리면 프록시가 먼저 끊는다(502). 뒤에서 돌리고
# 상태(task)를 화면이 다시 읽는다. 저장소마다 한 번에 하나.
_tasks: dict[str, threading.Thread] = {}


def busy() -> dict:
    """뒤에서 도는 내용 분석·제안 작업 수(이 프로세스) — 대시보드가 백엔드의 여유를 볼 때."""
    with _lock:
        return {"analysis": sum(1 for t in _workers.values() if t.is_alive()),
                "tasks": sum(1 for t in _tasks.values() if t.is_alive())}


def task_running(store_name: str) -> bool:
    task = _tasks.get(store_name)
    return task is not None and task.is_alive()


def submit(store_name: str, label: str, fn) -> bool:
    """fn(db, report)를 뒤에서 돌린다 — 이미 무엇이 돌고 있으면 False. report(문구, 단계, 단계 수)가 task에 남아
    화면 트레이가 진행을 보인다(폴더 업로드와 같은 자리)."""
    from ..db import SessionLocal  # noqa: PLC0415

    with _lock:
        if task_running(store_name):
            return False
        started = _now()

        def put(**extra):
            conn = _conn(store_name)
            try:
                _put(conn, "task", {"label": label, "started_at": started, **extra})
            finally:
                conn.close()

        # 스레드보다 **먼저** 남긴다 — 화면이 곧바로 읽어도 지난 작업의 「끝」을 이번 것으로 보지 않게.
        put(done=False, progress="시작하는 중", step=0, steps=None)

        def report(text: str, step: int | None = None, steps: int | None = None) -> None:
            try:
                put(done=False, progress=text, step=step, steps=steps)
            except Exception:  # noqa: BLE001 — 진행 문구 하나 때문에 작업을 실패로 끝내지 않는다(다음 보고가 덮는다)
                pass

        def run():
            db = SessionLocal()
            error = None
            try:
                fn(db, report)
            except Exception as e:  # noqa: BLE001 — 사유를 화면에 남긴다
                error = f"{e}"[:500]
            finally:
                db.close()
            put(done=True, error=error, finished_at=_now(), progress="실패" if error else "끝났습니다")

        thread = threading.Thread(target=run, daemon=True, name=f"semantic-task-{store_name}")
        _tasks[store_name] = thread
        thread.start()
        return True


# --- ④ 묶기 ---

def canonical_map(conn) -> dict[tuple[str, str], str]:
    """별칭 → 대표 이름, **끝까지 따라가서.** 묶기는 여러 번 돈다 — 앞에서 A-Electronics → 에이전자로 묶고, 양식
    문서가 들어온 뒤 에이전자 → 에이전자(주)로 묶이면 별칭이 사슬이 된다. 한 단계만 따르면 A-Electronics가
    별칭인 이름에 붙어 한 대상이 둘로 갈린다(실측). 고리는 끊는다."""
    direct = {(r["cls"], r["alias"]): r["canonical"] for r in conn.execute("SELECT cls, alias, canonical FROM sem_aliases")}
    resolved = {}
    for (cls, alias), canonical in direct.items():
        seen = {alias}
        while (cls, canonical) in direct and canonical not in seen:
            seen.add(canonical)
            canonical = direct[(cls, canonical)]
        resolved[(cls, alias)] = canonical
    return {key: value for key, value in resolved.items() if key[1] != value}


def merge_rules(conn) -> dict:
    """묶기 기준(JSON) — 확정한 것이 없으면 기본값. 판(version)은 내용으로 정한다."""
    import copy  # noqa: PLC0415
    from . import semantic_merge as sm  # noqa: PLC0415

    rules = _get(conn, "merge_rules") or copy.deepcopy(sm.DEFAULT_RULES)
    return {**rules, "version": sm.rules_version({k: v for k, v in rules.items() if k != "version"})}


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _record_pair(conn, cls: str, a: str, b: str, decision: str, source: str, score: float = 0.0,
                 reason: str = "") -> None:
    a, b = _pair_key(a, b)
    conn.execute("INSERT OR REPLACE INTO sem_pairs (cls, a, b, decision, source, score, reason, decided_at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (cls, a, b, decision, source, score, reason[:200], _now()))
    conn.commit()   # 바로 — LLM을 기다리는 동안 쓰기 잠금을 쥐지 않는다


def propose_merges(db: Session, store_name: str, report=_quiet) -> dict:
    """④ 같은 개체 묶기 — 형식화한 기준(merge_rules)으로 결정론적으로 정하고, 애매한 짝(gray)만 LLM이 판정·검토한다.

    LLM이 「같다」고 한 짝은 검토를 거쳐야 묶인다(합치기는 되돌리기 어렵다 — 되돌리면 사람의 「다르다」로 남는다).
    판정한 짝에서 LLM이 새 음역·약칭 규칙을 제안하면 초안으로 남긴다(merge_rules_draft — 확정은 관리자)."""
    from . import semantic_merge as sm  # noqa: PLC0415

    provider = provider_for(db, store_name)
    conn = _conn(store_name)
    try:
        rules = merge_rules(conn)
        names_by_cls: dict[str, dict[str, int]] = {}
        evidence: dict[tuple[str, str], list[str]] = {}
        for r in conn.execute("SELECT cls, name, path, evidence FROM sem_nodes"):
            names_by_cls.setdefault(r["cls"], {}).setdefault(r["name"], set()).add(r["path"])
            evidence.setdefault((r["cls"], r["name"]), []).append(r["evidence"])
        names_by_cls = {c: {n: len(ps) for n, ps in names.items()} for c, names in names_by_cls.items()}
        known = {(r["cls"], r["a"], r["b"]): (r["decision"], r["source"])
                 for r in conn.execute("SELECT cls, a, b, decision, source FROM sem_pairs")}
        stats = {"pairs": 0, "id": 0, "rule": 0, "llm": 0, "human": 0, "cached": 0, "gray": 0, "llm_calls": 0}
        applied, rejected = [], []
        llm_same: list[dict] = []

        for n, (cls, names) in enumerate(names_by_cls.items(), 1):
            report(f"'{cls}' 짝 고르기·판정 중(이름 {len(names)})", n, len(names_by_cls))
            ids = {name: sm.ids_of(evidence.get((cls, name), []), rules) for name in names}
            gray: list[tuple[str, str, float]] = []
            for a, b in sorted(sm.candidate_pairs(list(names), rules, ids)):
                stats["pairs"] += 1
                cached = known.get((cls, a, b))
                if cached and cached[1] in ("human", "llm"):
                    stats["cached" if cached[1] == "llm" else "human"] += 1
                    continue   # 사람·LLM이 이미 정한 짝은 다시 묻지 않는다(기준이 바뀌어도 그 판정이 이긴다)
                decision, source, value = sm.decide(a, b, rules, ids)
                if decision == "gray":
                    gray.append((a, b, value))
                    continue
                stats[source] += 1
                if not cached or cached[0] != decision:
                    _record_pair(conn, cls, a, b, decision, source, value,
                                 "같은 식별자" if source == "id" and decision == "same" else
                                 "서로 다른 식별자" if source == "id" else f"점수 {value:.2f}")
            stats["gray"] += len(gray)
            for k in range(0, len(gray), sm.LLM_BATCH):
                batch = gray[k:k + sm.LLM_BATCH]
                report(f"'{cls}' 애매한 짝 LLM 판정 {k + 1}~{k + len(batch)}/{len(gray)}", n, len(names_by_cls))
                judged = _judge_pairs(db, provider, cls, batch, evidence, "judge")
                stats["llm_calls"] += 1
                same = [i for i in range(len(batch)) if judged.get(i, (False, ""))[0]]
                checked = _judge_pairs(db, provider, cls, [batch[i] for i in same], evidence, "review") if same else {}
                stats["llm_calls"] += 1 if same else 0
                for i, (a, b, value) in enumerate(batch):
                    ok, why = judged.get(i, (False, "판정 없음"))
                    if ok:
                        ok, why2 = checked.get(same.index(i), (False, "검토 없음"))
                        why = why2 or why
                    _record_pair(conn, cls, a, b, "same" if ok else "different", "llm", value, why)
                    stats["llm"] += 1
                    (llm_same if ok else rejected).append({"cls": cls, "a": a, "b": b, "reason": why[:200]})

            # 묶음 다시 짓기 — 「같다」 짝으로 잇되, 「다르다」(사람·식별자)인 둘이 한 묶음이 되지 않게. 믿을 만한 것부터
            # 잇는다 — 문턱을 넘은 기준(rule)이 애매해서 LLM에 물은 것(llm)보다 앞선다
            rows = list(conn.execute("SELECT a, b, decision, source FROM sem_pairs WHERE cls = ?", (cls,)))
            rank = {"human": 0, "id": 1, "rule": 2, "llm": 3}
            same_pairs = sorted(((r["a"], r["b"], r["source"]) for r in rows if r["decision"] == "same"),
                                key=lambda x: rank.get(x[2], 9))
            cannot = {(r["a"], r["b"]) for r in rows if r["decision"] == "different" and r["source"] in ("human", "id")}
            mapping = _constrained_groups(names, same_pairs, cannot)
            old = {r["alias"]: r["canonical"] for r in conn.execute(
                "SELECT alias, canonical FROM sem_aliases WHERE cls = ?", (cls,))}
            # 별칭마다 근거(가장 믿을 만한 짝의 출처)를 **쓰기 전에** 정한다 — 예전에는 지우기로 쓰기 잠금을 잡은 뒤 별칭마다
            # 짝 전체를 훑어(별칭 × 짝) 그동안 다른 쓰기가 막혔다
            source_of: dict[str, str] = {}
            for a, b, src in same_pairs:
                source_of.setdefault(a, src)
                source_of.setdefault(b, src)
            rows_out = [(cls, alias, canonical, {"id": "같은 식별자", "rule": "이름 기준", "llm": "LLM 판정·검토",
                                                 "human": "사람이 확인"}.get(source_of.get(alias, "rule"), source_of.get(alias, "rule")))
                        for alias, canonical in mapping.items()]
            conn.execute("DELETE FROM sem_aliases WHERE cls = ?", (cls,))
            conn.executemany("INSERT INTO sem_aliases (cls, alias, canonical, reason) VALUES (?, ?, ?, ?)", rows_out)
            conn.commit()
            applied += [{"cls": cls, "alias": alias, "canonical": canonical, "reason": source_of.get(alias, "rule")}
                        for alias, canonical in mapping.items() if old.get(alias) != canonical]

        draft = _propose_merge_rules(db, provider, rules, llm_same) if llm_same else None
        if draft:
            _put(conn, "merge_rules_draft", draft)
            stats["llm_calls"] += 1
        result = {"applied": applied, "rejected": rejected, "stats": stats, "rules_version": rules["version"],
                  "at": _now()}
        _put(conn, "last_merge", result)
        return result
    finally:
        conn.close()


def _constrained_groups(names: dict[str, int], same_pairs: list[tuple[str, str, str]],
                        cannot: set[tuple[str, str]]) -> dict[str, str]:
    """「같다」를 믿음 순(same_pairs의 차례)으로 이어 묶음을 짓되, 「다르다」(cannot)인 둘이 같은 묶음이 되는 이음은 버린다."""
    member = {n: {n} for n in names}
    for a, b, _src in same_pairs:
        if a not in member or b not in member or member[a] is member[b]:
            continue
        ga, gb = member[a], member[b]
        if any(_pair_key(x, y) in cannot for x in ga for y in gb):
            continue
        ga |= gb
        for n in gb:
            member[n] = ga
    out: dict[str, str] = {}
    seen: set[int] = set()
    for group in member.values():
        if id(group) in seen or len(group) < 2:
            continue
        seen.add(id(group))
        head = max(group, key=lambda n: (names[n], -len(n), n))
        out.update({n: head for n in group if n != head})
    return out


def _judge_pairs(db, provider, cls: str, batch: list[tuple[str, str, float]], evidence: dict,
                 mode: str) -> dict[int, tuple[bool, str]]:
    """애매한 짝들을 한 번에 — 짝마다 같은 대상인지. mode=review면 다른 사람이 「같다」고 한 것을 검토한다."""
    items = [{"i": i, "a": a, "b": b, "a_근거": (evidence.get((cls, a)) or [""])[0][:160],
              "b_근거": (evidence.get((cls, b)) or [""])[0][:160]} for i, (a, b, _s) in enumerate(batch)]
    task = (f"'{cls}' 개념의 이름 짝마다 **같은 대상**인지 판정한다(약칭·영문·한글 표기·오타). 비슷하지만 다른 대상"
            "(다른 회사·다른 품목)은 다르다. 확신이 없으면 다르다." if mode == "judge" else
            f"다른 사람이 '{cls}' 개념의 이름 짝들을 같은 대상이라고 했다. 짝마다 정말 같은지 검토한다. 확신이 없으면 다르다.")
    out = _ask(db, provider, task + '\nJSON: {"pairs":[{"i":0,"same":true,"reason":""}]}\n' + _KO,
               json.dumps(items, ensure_ascii=False))
    return {int(p["i"]): (p.get("same") is True, str(p.get("reason", ""))) for p in out.get("pairs") or []
            if isinstance(p, dict) and str(p.get("i", "")).isdigit()}


def _propose_merge_rules(db, provider, rules: dict, same: list[dict]) -> dict | None:
    """LLM이 같다고 판정한 짝에서 **일반 규칙**(음역·떼어 낼 꼬리)을 뽑아 초안으로 — 다음부터 결정론적으로 처리한다."""
    try:
        out = _ask(db, provider,
            "같은 대상으로 판정된 이름 짝들에서, 다음부터 규칙으로 처리할 수 있는 **일반 규칙**만 뽑는다: 음역(영문 약칭 →"
            " 한글 표기, 예: lg → 엘지)과 떼어 낼 꼬리(회사 형태 등). 한 짝에만 맞는 규칙은 내지 않는다.\n"
            'JSON: {"translit":{"영문 소문자":"한글"},"strip":["떼어 낼 말"]}\n' + _KO,
            json.dumps({"pairs": same[:60], "현재 음역표": rules.get("translit"), "현재 꼬리": rules.get("strip")},
                       ensure_ascii=False))
    except Exception:  # noqa: BLE001 — 규칙 제안은 덤이다: 실패해도 묶기는 끝났다
        return None
    translit = {str(k).lower(): str(v) for k, v in (out.get("translit") or {}).items()
                if str(k).lower() not in rules.get("translit", {})}
    strip = [str(x) for x in out.get("strip") or [] if str(x) not in rules.get("strip", [])]
    if not translit and not strip:
        return None
    return {"translit": translit, "strip": strip, "from_pairs": len(same), "created_at": _now()}


def confirm_merge_rules(store_name: str, rules: dict, actor: str) -> dict:
    """관리자가 기준을 확정 — 다음 묶기부터 쓴다(판이 바뀐다). 초안은 비운다."""
    from . import semantic_merge as sm  # noqa: PLC0415

    clean = {k: rules[k] for k in sm.DEFAULT_RULES if k in rules}
    for pattern in clean.get("id_patterns", []):
        try:
            re.compile(pattern)
        except re.error as e:
            raise SemanticError(f"식별자 패턴이 정규식이 아닙니다: {pattern} ({e})") from e
    th = clean.get("thresholds") or {}
    if not 0 <= th.get("reject", 0) < th.get("merge", 1) <= 1:
        raise SemanticError("문턱은 0 ≤ 거절 < 묶음 ≤ 1 이어야 합니다.")
    conn = _conn(store_name)
    try:
        merged = {**merge_rules(conn), **clean}
        merged.pop("version", None)
        _put(conn, "merge_rules", {**merged, "confirmed_by": actor, "confirmed_at": _now()})
        conn.execute("DELETE FROM sem_meta WHERE key = 'merge_rules_draft'")
        conn.commit()
        return merge_rules(conn)
    finally:
        conn.close()


def remove_alias(store_name: str, cls: str, alias: str, actor: str = "") -> None:
    """묶음 풀기 — 사람이 「다르다」고 한 것으로 남긴다. 그래야 다음 묶기가 기준(정규화 등)으로 다시 묶지 않는다."""
    conn = _conn(store_name)
    try:
        row = conn.execute("SELECT canonical FROM sem_aliases WHERE cls = ? AND alias = ?", (cls, alias)).fetchone()
        conn.execute("DELETE FROM sem_aliases WHERE cls = ? AND alias = ?", (cls, alias))
        conn.commit()
        if row:
            _record_pair(conn, cls, alias, row["canonical"], "different", "human", 0.0, f"사람이 풀었다 {actor}".strip())
    finally:
        conn.close()


# --- ⑤ 사람 정답·기준 다시 배우기 ---
# 정답의 뜻: 짝(pair)은 「같은 대상인가」, 개체(node)·관계(edge)는 「맞게 뽑았나」(1·0). 기준을 다시 배우고(semantic_tune)
# 규칙의 정밀도를 재는 근거다 — LLM 판정은 약한 정답일 뿐이다.

_TAB = "char(9)"
_GOLD_SQL = {
    "pair": ("SELECT cls, a, b, decision, source, score, reason FROM sem_pairs x WHERE source != 'human'",
             f"x.cls || {_TAB} || x.a || {_TAB} || x.b"),
    "node": ("SELECT path, cls, name, evidence, origin FROM sem_nodes x WHERE 1 = 1",
             f"x.path || {_TAB} || x.cls || {_TAB} || x.name"),
    "edge": ("SELECT path, src_cls, src, rel, dst_cls, dst, evidence, origin FROM sem_edges x WHERE 1 = 1",
             f"x.path || {_TAB} || x.src_cls || {_TAB} || x.src || {_TAB} || x.rel || {_TAB} || x.dst_cls"
             f" || {_TAB} || x.dst"),
}


def _gold_key(kind: str, row: dict) -> str:
    cols = {"pair": ("cls", "a", "b"), "node": ("path", "cls", "name"),
            "edge": ("path", "src_cls", "src", "rel", "dst_cls", "dst")}[kind]
    return "\t".join(row[c] for c in cols)


GOLD_AUDIT_SHARE = 0.3    # ⑤ 무작위 점검의 몫 — 기계가 확신한 것도 맞는지 재야 기준 배우기가 정확도를 잰다(나머지는 불확실한 것)


def gold_queue(store_name: str, kind: str, n: int = 10) -> list[dict]:
    """사람이 확인할 것 — 아직 확인하지 않은 것에서, **불확실한 것부터**. 항목마다 묻는 까닭(why)을 단다.

      짝     점수가 애매한 것(0.5 가까이 — 그것을 알아야 문턱이 움직인다) 70% · 무작위 점검 30%. 같은 식별자로 정한 짝은
             점검에서도 뺀다(코드가 같다 — 물을 것이 없다)
      개체·관계  ① 근거 문장에 이름이 없던 LLM 추출 ② 폴더와 내용이 어긋난 문서의 것(개체) ③ 한 문서에만 나온 LLM 추출 —
             그다음 무작위 점검 30%. 경로 틀(path)이 단 개체는 묻지 않는다(확정한 틀의 규칙이다 — 틀을 고칠 일이다)

    예전에는 짝의 절반, 개체·관계의 전부를 무작위로 물어 확실한 것을 사람이 다시 확인하는 일이 많았다."""
    if kind not in GOLD_KINDS:
        raise SemanticError(f"kind는 {', '.join(GOLD_KINDS)} 중 하나입니다.")
    select, key = _GOLD_SQL[kind]
    fresh = f" AND NOT EXISTS (SELECT 1 FROM sem_gold g WHERE g.kind = '{kind}' AND g.item = {key})"
    audit = max(1, round(n * GOLD_AUDIT_SHARE))
    conn = _conn(store_name)
    try:
        picked: list[tuple[dict, str]] = []

        def take(sql: str, args: tuple, why: str, limit: int) -> None:
            if limit > 0:
                picked.extend((dict(r), why) for r in conn.execute(select + fresh + sql + " LIMIT ?", (*args, limit)))

        if kind == "pair":
            take(" ORDER BY ABS(score - 0.5)", (), "애매한 짝(점수가 문턱 사이)", n - audit)
            take(" AND x.source != 'id' ORDER BY RANDOM()", (), "점검(무작위)", audit)
        else:
            base = " AND x.origin != 'path'"
            take(base + " AND x.origin = 'llm' AND x.anchored = '0' ORDER BY RANDOM()", (),
                 "근거 문장에 이름이 없음", n - audit)
            if kind == "node":
                confirmed = (_get(conn, "paths", {}) or {}).get("confirmed")
                stuck = _path_mismatches(conn, confirmed).get("paths") or []
                if stuck:
                    marks = ",".join("?" * len(stuck))
                    take(base + f" AND x.path IN ({marks}) ORDER BY RANDOM()", tuple(stuck),
                         "폴더와 내용이 어긋난 문서", n - audit - len(picked))
                take(base + " AND x.origin = 'llm' AND (SELECT COUNT(DISTINCT y.path) FROM sem_nodes y"
                     " WHERE y.cls = x.cls AND y.name = x.name) = 1 ORDER BY RANDOM()", (),
                     "한 문서에만 나옴", n - audit - len(picked))
            else:
                take(base + " AND x.origin = 'llm' AND (SELECT COUNT(DISTINCT y.path) FROM sem_edges y"
                     " WHERE y.src = x.src AND y.rel = x.rel AND y.dst = x.dst) = 1 ORDER BY RANDOM()", (),
                     "한 문서에만 나옴", n - audit - len(picked))
            take(base + " ORDER BY RANDOM()", (), "점검(무작위)", n - len(picked))
        out, seen = [], set()
        for r, why in picked:
            item = _gold_key(kind, r)
            if item in seen:
                continue
            seen.add(item)
            if kind == "pair":       # 짝은 두 이름의 근거를 함께 — 이름만으로는 사람도 모른다
                for side in ("a", "b"):
                    ev = conn.execute("SELECT evidence, path FROM sem_nodes WHERE cls = ? AND name = ? LIMIT 1",
                                      (r["cls"], r[side])).fetchone()
                    r[f"{side}_evidence"] = dict(ev) if ev else None
            out.append({"kind": kind, "item": item, "why": why, **r})
        return out[:n]
    finally:
        conn.close()


def label_gold(store_name: str, kind: str, item: str, label: bool, actor: str) -> dict:
    """정답 하나 — 짝이면 사람의 판정으로도 남기고(다음 묶기에 바로 쓰인다), 틀린 개체·관계는 그래프에서 뺀다."""
    if kind not in GOLD_KINDS:
        raise SemanticError(f"kind는 {', '.join(GOLD_KINDS)} 중 하나입니다.")
    select, key = _GOLD_SQL[kind]
    conn = _conn(store_name)
    try:
        row = conn.execute(select.replace(" WHERE source != 'human'", " WHERE 1 = 1") + f" AND {key} = ?",
                           (item,)).fetchone()
        if row is None:
            raise SemanticError("그 항목이 없습니다 — 다시 분석·묶기로 바뀌었을 수 있습니다.")
        detail = dict(row)
        if kind != "pair" and detail.get("origin") == "template":
            body = conn.execute("SELECT body FROM docs WHERE path = ?", (detail["path"],)).fetchone()
            skeletons = {tid: t["skeleton"] for tid, t in _get(conn, "templates", {}).items()}
            detail["template"] = st.member_of(st.lines_of((body and body["body"]) or ""), skeletons)
        conn.execute("INSERT OR REPLACE INTO sem_gold (kind, item, label, detail, labeled_by, labeled_at)"
                     " VALUES (?, ?, ?, ?, ?, ?)",
                     (kind, item, int(label), json.dumps(detail, ensure_ascii=False), actor, _now()))
        conn.commit()
        if kind == "pair":
            _record_pair(conn, row["cls"], row["a"], row["b"], "same" if label else "different", "human",
                         row["score"], f"사람이 확인 {actor}".strip())
        elif not label:
            conn.execute(f"DELETE FROM {'sem_nodes' if kind == 'node' else 'sem_edges'} AS x WHERE {key} = ?", (item,))
            conn.commit()
        return gold_metrics(conn)
    finally:
        conn.close()


def gold_metrics(conn) -> dict:
    """정답으로 잰 정확도 — 짝은 출처(id·rule·llm)별로 그 판정이 맞았나, 개체·관계는 출처(llm·form·template)별 정밀도."""
    out: dict = {k: {"labeled": 0, "by": {}} for k in GOLD_KINDS}
    for r in conn.execute("SELECT kind, label, detail FROM sem_gold"):
        d = json.loads(r["detail"])
        m = out[r["kind"]]
        m["labeled"] += 1
        if r["kind"] == "pair":
            by, ok = d.get("source", ""), (d.get("decision") == "same") == bool(r["label"])
        else:
            by, ok = d.get("origin", ""), bool(r["label"])
        b = m["by"].setdefault(by, {"n": 0, "correct": 0})
        b["n"] += 1
        b["correct"] += ok
    for m in out.values():
        for b in m["by"].values():
            b["accuracy"] = b["correct"] / b["n"]
    return out


def _revalidate_templates(conn, store_name: str) -> list[dict]:
    """확정한 템플릿 규칙을 다시 잰다 — 검증 문서(지금 글)와 사람 정답. 기준 밑이면 멈춘다(그 문서들은 LLM으로)."""
    saved = _get(conn, "templates", {})
    golds: dict[str, list[int]] = {}
    for r in conn.execute("SELECT label, detail FROM sem_gold WHERE kind IN ('node', 'edge')"):
        d = json.loads(r["detail"])
        if d.get("origin") == "template" and d.get("template"):
            golds.setdefault(d["template"], []).append(r["label"])
    out = []
    for tid, t in saved.items():
        conf = t.get("confirmed")
        if not conf:
            continue
        check = []
        for c in (t.get("draft") or {}).get("check", []):
            try:
                check.append({**c, "text": _markdown(store_name, c["path"])[:DOC_CHARS]})
            except Exception:  # noqa: BLE001 — 지워진 검증 문서는 빼고 잰다
                continue
        by_check = st.evaluate(conf["rules"], check)["precision"] if check else None
        labels = golds.get(tid, [])
        by_human = sum(labels) / len(labels) if len(labels) >= TEMPLATE_GOLD_MIN else None
        low = [f"{name} {v:.0%}" for name, v in (("검증 문서", by_check), ("사람 정답", by_human))
               if v is not None and v < TEMPLATE_PRECISION]
        if low:
            t["suspended"] = {**conf, "reason": f"{' · '.join(low)} — 기준 {TEMPLATE_PRECISION:.0%} 밑", "at": _now()}
            t["confirmed"] = None
        out.append({"id": tid, "status": "suspended" if low else "kept", "check_precision": by_check,
                    "human_precision": by_human, "human_labels": len(labels)})
    if any(o["status"] == "suspended" for o in out):
        _put(conn, "templates", saved)
    return out


def tune(store_name: str, actor: str = "scheduler") -> dict:
    """기준 다시 배우기 — 묶기 가중치·문턱(나을 때만 바꾼다, 이전 기준은 기록에 남긴다)과 템플릿 규칙 재검증."""
    from . import semantic_tune as tu  # noqa: PLC0415

    conn = _conn(store_name)
    try:
        rules = merge_rules(conn)
        human: dict[tuple[str, str], int] = {}
        for r in conn.execute("SELECT item, label FROM sem_gold WHERE kind = 'pair'"):
            _cls, a, b = r["item"].split("\t")
            human[(a, b)] = r["label"]
        for r in conn.execute("SELECT a, b, decision FROM sem_pairs WHERE source = 'human'"):
            human.setdefault((r["a"], r["b"]), int(r["decision"] == "same"))
        weak = [(r["a"], r["b"], int(r["decision"] == "same"), r["source"])
                for r in conn.execute("SELECT a, b, decision, source FROM sem_pairs WHERE source IN ('llm', 'id')")]
        merge = tu.tune_merge(rules, human, weak)
        if merge["status"] == "promoted":
            merge["previous"] = {k: v for k, v in rules.items() if k != "version"}
            _put(conn, "merge_rules", {**{k: v for k, v in merge.pop("rules").items() if k != "version"},
                                       "tuned_by": actor, "tuned_at": _now()})
            merge["version"] = merge_rules(conn)["version"]
        entry = {"at": _now(), "actor": actor, "merge": merge, "templates": _revalidate_templates(conn, store_name),
                 "gold": gold_metrics(conn)}
        _put(conn, "tune_history", (_get(conn, "tune_history", []) + [entry])[-30:])
        return entry
    finally:
        conn.close()


def has_schema(store_name: str) -> bool:
    """확정한 스키마가 있나 — 스케줄러가 tick마다 묻는다. 읽기만 한다(표를 만들지 않는다). 어떤 실패든 False —
    여기서 예외가 나면 그 tick의 다른 job이 모두 멈춘다."""
    import sqlite3  # noqa: PLC0415

    path = docsearch.index_path(store_name)
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(path, timeout=1.0)
        try:
            return conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sem_meta'").fetchone() is not None and \
                conn.execute("SELECT 1 FROM sem_meta WHERE key = 'schema'").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


# --- 조회 ---

def _canon(aliases: dict, cls: str, name: str) -> str:
    return aliases.get((cls, name), name)


def status(store_name: str) -> dict:
    """화면이 읽는 것 전부 — 스키마(초안·확정), 양식, 분석 진행, 표본 검토, 스키마 밖 후보, 묶기."""
    conn = _conn(store_name)
    try:
        docs = conn.execute("SELECT COUNT(*) AS n FROM docs WHERE body IS NOT NULL").fetchone()["n"]
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM sem_docs GROUP BY status")}
        state = _get(conn, "state", {})
        items = state.get("review_items", 0)
        return {
            "store": store_name, "documents": docs, "analyzed": by_status, "running": running(store_name),
            "schema_draft": _get(conn, "schema_draft"), "schema": _get(conn, "schema"),
            "paused": state.get("paused"),
            "review": {"items": items, "wrong": state.get("review_wrong", 0),
                       "error_rate": (state.get("review_wrong", 0) / items) if items else None,
                       "stop_rate": STOP_ERROR_RATE, "notes": state.get("review_notes", []),
                       "check_items": state.get("check_items", 0), "check_wrong": state.get("check_wrong", 0)},
            "last_run": _get(conn, "last_run"), "outside": _get(conn, "outside", {}),
            "task": {**(_get(conn, "task") or {}), "running": task_running(store_name)},
            "last_merge": _get(conn, "last_merge"), "batch_proposals": _get(conn, "batch_proposals"),
            "merge_rules": merge_rules(conn), "merge_rules_draft": _get(conn, "merge_rules_draft"),
            "gold": gold_metrics(conn), "tune_history": _get(conn, "tune_history", [])[-5:],
            "pairs": {f'{r["source"]}:{r["decision"]}': r["n"] for r in conn.execute(
                "SELECT source, decision, COUNT(*) AS n FROM sem_pairs GROUP BY source, decision")},
            "aliases": [dict(r) for r in conn.execute("SELECT cls, alias, canonical, reason FROM sem_aliases ORDER BY cls, canonical")],
            "failed": [dict(r) for r in conn.execute(
                "SELECT path, error FROM sem_docs WHERE status = 'failed' ORDER BY path LIMIT 20")],
            "layers": LAYERS,
            "counts": {
                "nodes": {r["cls"]: r["n"] for r in conn.execute("SELECT cls, COUNT(*) AS n FROM sem_nodes GROUP BY cls")},
                "edges": {r["rel"]: r["n"] for r in conn.execute("SELECT rel, COUNT(*) AS n FROM sem_edges GROUP BY rel")},
            },
        }
    finally:
        conn.close()


def search(store_name: str, cls: str = "", q: str = "", limit: int = 40) -> list[dict]:
    """개체 찾기 — 별칭을 대표 이름으로 묶어서, 많은 문서에 나오는 것부터."""
    conn = _conn(store_name)
    try:
        aliases = canonical_map(conn)
        groups: dict[tuple[str, str], dict] = {}
        for r in conn.execute("SELECT cls, name, path, evidence, origin FROM sem_nodes"):
            key = (r["cls"], _canon(aliases, r["cls"], r["name"]))
            if cls and key[0] != cls:
                continue
            g = groups.setdefault(key, {"cls": key[0], "name": key[1], "paths": set(), "names": set(), "ids": set()})
            g["paths"].add(r["path"])
            g["names"].add(r["name"])
            if r["origin"] == "path":       # 경로 틀이 단 식별자(협력사 코드) — 저장소 사이를 잇는 열쇠
                g["ids"].update(_PATH_ID.findall(r["evidence"] or ""))
        needle = q.strip().lower()
        out = [{"cls": g["cls"], "name": g["name"], "documents": len(g["paths"]),
                "aliases": sorted(g["names"] - {g["name"]}), **({"ids": sorted(g["ids"])} if g["ids"] else {})}
               for g in groups.values()
               if not needle or any(needle in n.lower() for n in g["names"] | g["ids"])]
        return sorted(out, key=lambda g: (-g["documents"], g["name"]))[:limit]
    finally:
        conn.close()


def _in_force(r, as_of: str) -> bool:
    """as_of 기준으로 참이었나 — 그날까지 본 것이고, 효력 기간이 있으면 그 안."""
    return ((not r["observed_at"] or r["observed_at"] <= as_of) and (not r["valid_from"] or r["valid_from"] <= as_of)
            and (not r["valid_to"] or r["valid_to"] >= as_of))


def neighbors(store_name: str, cls: str, name: str, limit: int = 60, as_of: str = "") -> dict:
    """한 개체의 관계 — 관계·상대마다 묶고, 근거 문장과 문서를 함께(원문으로 되짚을 수 있게).

    시점에 매인 관계(스키마 temporal — 상태·사건)는 같은 관계·방향의 상대가 여럿이면 **가장 늦게 본 것**이 current다
    (신용등급 A → BBB면 BBB). as_of(YYYY-MM-DD)를 주면 그날 기준으로 참이던 것만 — 그날까지 본 것, 효력 기간 안."""
    conn = _conn(store_name)
    try:
        schema = _get(conn, "schema") or {}
        temporal = temporal_rels(schema) if schema else set()
        layers = layer_of(schema) if schema else {}
        as_of = iso_date(as_of) if as_of else ""
        aliases = canonical_map(conn)
        names = {name} | {a for (c, a), canon in aliases.items() if c == cls and canon == name}
        marks = ",".join("?" * len(names))
        rows = list(conn.execute(
            f"SELECT * FROM sem_edges WHERE (src_cls = ? AND src IN ({marks})) OR (dst_cls = ? AND dst IN ({marks}))"
            " ORDER BY observed_at DESC", (cls, *names, cls, *names)))
        if as_of:
            rows = [r for r in rows if _in_force(r, as_of)]
        grouped: dict[tuple, dict] = {}
        for r in rows:
            outgoing = r["src_cls"] == cls and r["src"] in names
            other_cls, other = (r["dst_cls"], r["dst"]) if outgoing else (r["src_cls"], r["src"])
            other = _canon(aliases, other_cls, other)
            key = (r["rel"], "out" if outgoing else "in", other_cls, other)
            g = grouped.setdefault(key, {"rel": r["rel"], "direction": key[1], "cls": other_cls, "name": other,
                                         "layer": layers.get(other_cls, ""), "temporal": r["rel"] in temporal,
                                         "latest": "", "evidence": []})
            g["latest"] = max(g["latest"], r["observed_at"] or "")
            if len(g["evidence"]) < 5:
                g["evidence"].append({"path": r["path"], "text": r["evidence"], "origin": r["origin"],
                                      "observed_at": r["observed_at"], "valid_from": r["valid_from"],
                                      "valid_to": r["valid_to"]})
        # 시점에 매인 관계 — 같은 관계·방향에서 가장 늦게 본 상대가 지금(current) 값이다
        newest: dict[tuple, str] = {}
        for g in grouped.values():
            if g["temporal"]:
                k = (g["rel"], g["direction"])
                newest[k] = max(newest.get(k, ""), g["latest"])
        for g in grouped.values():
            if g["temporal"]:
                g["current"] = g["latest"] == newest[(g["rel"], g["direction"])]
        mentions = [dict(r) for r in conn.execute(
            f"SELECT path, evidence, origin, observed_at FROM sem_nodes WHERE cls = ? AND name IN ({marks})"
            " ORDER BY observed_at DESC LIMIT 10", (cls, *names))]
        relations = sorted(grouped.values(), key=lambda g: (-int(bool(g.get("current"))), -len(g["evidence"])))[:limit]
        return {"cls": cls, "name": name, "layer": layers.get(cls, ""), "as_of": as_of, "relations": relations,
                "mentions": mentions}
    finally:
        conn.close()


# --- 재구성 ---

RESET_SCOPES = ("facts", "all")
_FACT_TABLES = ("sem_docs", "sem_nodes", "sem_edges")
_ALL_TABLES = (*_FACT_TABLES, "sem_aliases", "sem_pairs", "sem_gold", "sem_meta")
_FACT_KEYS = ("state", "last_run", "outside", "last_merge", "task")


def reset(store_name: str, scope: str, actor: str = "") -> dict:
    """내용 그래프를 지운다 — 층위·시점이 없던 옛 추출을 새 규칙으로 다시 만들 때.

      facts  추출 결과만(문서 분석·개체·관계·분석 상태). 확정한 스키마·양식·템플릿·묶기 기준·사람 정답은 둔다 —
             ③ 분석부터 다시 돈다(스키마에 층위가 없으면 ① 다시 확정하는 편이 낫다)
      all    확정한 것까지 전부(스키마·양식·템플릿·묶기·정답). ① 스키마 제안부터 다시

    형식 그래프의 판 정보(doc_meta — 시행일·개정 계열)도 비운다: docsearch.regraph가 .ready에서 다시 만든다.
    분석·제안이 돌고 있으면 지우지 않는다."""
    if scope not in RESET_SCOPES:
        raise SemanticError(f"scope는 {', '.join(RESET_SCOPES)} 중 하나입니다.")
    if running(store_name) or task_running(store_name):
        raise SemanticError("분석이나 제안이 돌고 있습니다 — 멈추고 끝난 뒤에 재구성하세요.")
    conn = _conn(store_name)
    try:
        tables = _FACT_TABLES if scope == "facts" else _ALL_TABLES
        removed = {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
        if scope == "facts":
            conn.execute(f"DELETE FROM sem_meta WHERE key IN ({','.join('?' * len(_FACT_KEYS))})", _FACT_KEYS)
        removed["doc_meta"] = conn.execute("SELECT COUNT(*) AS n FROM doc_meta").fetchone()["n"]
        conn.execute("DELETE FROM doc_meta")
        conn.commit()
        return {"scope": scope, "removed": removed, "by": actor, "at": _now()}
    finally:
        conn.close()


# --- 양식·템플릿을 한꺼번에 제안(사람은 확정만) ---

PROPOSE_LIMIT = 15


def propose_all(db: Session, store_name: str, report=_quiet, limit: int = PROPOSE_LIMIT) -> dict:
    """확정하지 않은(또는 옛 스키마의) 양식 대응표와 템플릿 규칙을 문서가 많은 것부터 limit개씩 제안·검토한다 — 확정은
    사람이 한다(②-1 양식·②-2 템플릿). 하나가 실패해도 나머지는 간다(사유를 남긴다). 재구성 뒤 분석 전에 쓴다: 확정하면 그
    문서들은 LLM 대신 규칙으로 뽑힌다."""
    conn = _conn(store_name)
    try:
        version = _schema(conn)["version"]
    finally:
        conn.close()

    def todo(items: list[dict], min_docs: int) -> list[dict]:
        return [i for i in items if i["documents"] >= min_docs and not i.get("draft")
                and not (i.get("confirmed") and i["confirmed"].get("schema_version") == version)][:limit]

    forms_todo = todo(forms(store_name), 1)
    templates_todo = todo(templates(store_name), TEMPLATE_MIN_DOCS)
    steps = len(forms_todo) + len(templates_todo)
    done, failed = [], []
    for k, f in enumerate(forms_todo):
        report(f"양식 대응 제안·검토 {k + 1}/{len(forms_todo)} · {f['name'][:60]}", k + 1, steps)
        try:
            propose_mapping(db, store_name, f["name"])
            done.append({"kind": "양식", "name": f["name"], "documents": f["documents"]})
        except Exception as e:  # noqa: BLE001
            failed.append({"kind": "양식", "name": f["name"], "error": str(e)[:200]})
    for k, t in enumerate(templates_todo):
        report(f"템플릿 규칙 제안·검증 {k + 1}/{len(templates_todo)} · {t['label'][:60]}", len(forms_todo) + k + 1, steps)
        try:
            propose_template(db, store_name, t["id"])
            done.append({"kind": "템플릿", "name": t["label"], "documents": t["documents"]})
        except Exception as e:  # noqa: BLE001
            failed.append({"kind": "템플릿", "name": t["label"], "error": str(e)[:200]})
    out = {"proposed": done, "failed": failed, "at": _now()}
    conn = _conn(store_name)
    try:
        _put(conn, "batch_proposals", out)
    finally:
        conn.close()
    return out
