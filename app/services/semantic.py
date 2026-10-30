"""내용 온톨로지 — 문서의 **형식**이 아니라 **내용**에서 개념과 관계를 뽑는다(LLM).

형식 그래프(services/ontology.py)는 제목·조문·표 머리글·「」 인용에서 나온다. 사실이지만, 같은 내용이라도
제목 스타일이나 표가 없으면 노드가 없고 "A사가 B 품목을 공급한다" 같은 관계는 아예 없다. 이 모듈이 그
관계를 만든다. 형식 그래프는 그대로 둔다 — 그것을 쓰는 곳(워크플로 검증·연결, 전환 현황)이 종류 4가지를
전제로 하고, LLM이 없거나 실패해도 남아야 한다. 저장은 같은 색인 파일의 따로 된 표다(sem_*).

네 단계이고, **한 번 정해 많은 문서에 퍼지는 결과는 LLM이 전부 검토**하고 문서 하나에 그치는 결과는 표본만
검토한다(모든 결과를 다시 읽으면 비용이 두 배다):

  ① 스키마   표본 문서 → 개념 종류 제안 → 검토 → 고침 → 관계는 개념 몇 개씩 묶어 차례로 제안 → 묶음마다 검토
             → 관리자 확정(판 번호가 생긴다). 관계 수에 상한을 두지 않는다
  ② 양식     되풀이되는 표(견적서 양식 하나가 320개 문서에 있다)는 LLM이 문서마다 읽지 않는다. 열이 어떤
             개념인지 대응표를 제안 → 실제 값 표본과 대조해 검토 → 관리자 확정 → **규칙으로** 전부 뽑는다
  ③ 문서     나머지 문서 → 추출 → 기계 검사(전부: 근거 문장이 원문에 있나·스키마에 있나) → 표본 검토(10%)
             → 표본 오류율이 기준을 넘으면 멈추고 알린다(스키마·지시가 틀린 것이라 계속 돌면 비용만 든다)
  ④ 묶기     이름 정규화로 같은 것을 묶고, 애매한 것은 LLM이 묶음을 제안 → 묶음마다 검토 → 통과한 것만 적용

제안과 검토를 같은 기본 LLM이 한다 — 같은 모델은 같은 실수를 놓칠 수 있어서, 원문·실제 값과 기계로
대조하는 검사를 함께 둔다(검토가 놓쳐도 그것은 걸린다).
"""
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from html import unescape

from sqlalchemy.orm import Session

from . import docsearch, docready, ontology
from . import llm as llm_service
from . import storage as storage_service

# 표본·추출에 넣는 문서 길이. 문서 전체를 넣으면 긴 규정 하나가 수십만 토큰이다 — 앞부분이 대개 목적·정의·
# 당사자를 말한다. 잘렸다는 사실은 기록한다(truncated).
SAMPLE_CHARS = 2_500
DOC_CHARS = 12_000
SAMPLE_DOCS = 12
# 이만큼 많은 문서에 되풀이되는 표가 "양식"이다.
FORM_MIN_DOCS = 5
FORM_SAMPLE_DOCS = 3
# 확정한 양식 표를 뺀 나머지 글이 이보다 짧으면 "양식 문서"다(견적서) — LLM을 부르지 않는다. 길면(신용평가서처럼
# 양식 표 몇 개를 품은 보고서) 나머지 글은 LLM이 읽는다. 문서 전체를 건너뛰면 그 본문의 관계가 통째로 빠진다(실측).
FORM_REST_CHARS = 1_500
FORM_SAMPLE_ROWS = 8
# 표본 검토 — 10%. 경로의 해시로 고른다(돌 때마다 같은 문서가 고르게 뽑힌다).
REVIEW_EVERY = 10
# 멈춤 조건 — 표본으로 이만큼 본 뒤에 틀린 비율이 기준을 넘으면.
STOP_MIN_ITEMS = 30
STOP_ERROR_RATE = 0.2
# 묶기 — 한 번에 LLM에게 보이는 이름 수(종류마다).
MERGE_NAMES = 150
# 관계는 한 번에 다 찾지 않는다 — 개념을 이만큼씩 묶어(그 개념에서 나가는 관계) 차례로 제안·검토한다. 관계 수에
# 상한을 두지 않는다: 업무가 넓은 저장소는 관계가 많고, 한 번에 다 내게 하면 상한에 걸리거나(실측: 40개 초과로
# 확정 불가) 뒤쪽이 대충 채워진다.
RELATION_BATCH = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sem_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sem_docs (
  path TEXT PRIMARY KEY, fp TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
  truncated INTEGER NOT NULL DEFAULT 0, dropped INTEGER NOT NULL DEFAULT 0,
  reviewed INTEGER NOT NULL DEFAULT 0, analyzed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sem_nodes (
  path TEXT NOT NULL, cls TEXT NOT NULL, name TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'llm'
);
CREATE TABLE IF NOT EXISTS sem_edges (
  path TEXT NOT NULL, src_cls TEXT NOT NULL, src TEXT NOT NULL, rel TEXT NOT NULL,
  dst_cls TEXT NOT NULL, dst TEXT NOT NULL, evidence TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'llm'
);
CREATE TABLE IF NOT EXISTS sem_aliases (
  cls TEXT NOT NULL, alias TEXT NOT NULL, canonical TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (cls, alias)
);
CREATE INDEX IF NOT EXISTS ix_sem_nodes_path ON sem_nodes(path);
CREATE INDEX IF NOT EXISTS ix_sem_nodes_name ON sem_nodes(cls, name);
CREATE INDEX IF NOT EXISTS ix_sem_edges_path ON sem_edges(path);
CREATE INDEX IF NOT EXISTS ix_sem_edges_src ON sem_edges(src_cls, src);
CREATE INDEX IF NOT EXISTS ix_sem_edges_dst ON sem_edges(dst_cls, dst)
"""


class SemanticError(RuntimeError):
    """사람이 읽을 사유 — 화면에 그대로 뜬다."""


def _conn(store_name: str):
    conn = docsearch._connect(store_name)
    conn.executescript(_SCHEMA)
    return conn


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


def _space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --- ① 스키마 ---

def _form_tables(conn) -> list[dict]:
    """되풀이되는 표(양식) — 표 머리글이 같은 문서가 FORM_MIN_DOCS 이상."""
    return [{"name": r["name"], "columns": r["name"].split(" | "), "documents": r["n"]}
            for r in conn.execute(
                "SELECT name, COUNT(DISTINCT path) AS n FROM nodes WHERE kind = 'table'"
                " GROUP BY name HAVING n >= ? ORDER BY n DESC, name", (FORM_MIN_DOCS,))]


def _sample(store_name: str, conn, n: int = SAMPLE_DOCS) -> list[dict]:
    """표본 문서 — 양식은 하나씩만(320건짜리 견적서가 표본을 다 먹지 않게), 나머지는 큰 것부터 고르게."""
    forms = {f["name"] for f in _form_tables(conn)}
    form_docs: dict[str, str] = {}
    for r in conn.execute("SELECT path, name FROM nodes WHERE kind = 'table'"):
        if r["name"] in forms:
            form_docs.setdefault(r["path"], r["name"])
    seen_forms: set[str] = set()
    picked: list[str] = []
    rows = list(conn.execute("SELECT path FROM docs WHERE body IS NOT NULL ORDER BY length(body) DESC"))
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
    'JSON: {"classes":[{"name":"","description":"","examples":[""]}]}'
)
_RELATION_RULES = (
    "관계 종류 — from·to는 개념 목록의 이름이고 방향이 뜻이다(협력사 —공급한다→ 품목). 표본에 근거가 있는 것만."
    " 이미 정한 관계와 뜻이 같으면 새 이름을 만들지 말고 **그 이름을 그대로** 쓴다(승인받는다·결재받는다를 둘로"
    " 두지 않는다).\n"
    'JSON: {"relations":[{"name":"","from":"","to":"","description":""}]}'
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
    return [{"name": str(c["name"]).strip(), "description": str(c.get("description", ""))[:300],
             "examples": [str(x)[:60] for x in (c.get("examples") or [])][:5]}
            for c in (classes or []) if isinstance(c, dict) and c.get("name")]


def _clean_relations(relations) -> list[dict]:
    return [{"name": str(r["name"]).strip(), "from": str(r.get("from", "")).strip(), "to": str(r.get("to", "")).strip(),
             "description": str(r.get("description", ""))[:300]}
            for r in (relations or []) if isinstance(r, dict) and r.get("name")]


def _clean_schema(schema: dict) -> dict:
    return {"classes": _clean_classes(schema.get("classes")), "relations": _clean_relations(schema.get("relations"))}


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
            found = _clean_relations(_ask(db, provider,
                f"아래 **기준 개념에서 나가는** 관계 종류를 찾는다(to는 개념 목록 어디든). {_RELATION_RULES}\n{_KO}",
                f"{material}\n\n개념 목록: {json.dumps(classes, ensure_ascii=False)}\n"
                f"기준 개념: {json.dumps(anchors, ensure_ascii=False)}\n"
                f"이미 정한 관계: {json.dumps(relations, ensure_ascii=False)}").get("relations"))
            found = [r for r in found if r["from"] in anchors and r["to"] in names]
            proposed += found
            if not found:
                continue
            report(f"관계 검토 {k + 1}/{len(batches)}({len(found)}개)", 6 + 2 * k, total)
            checked = _ask(db, provider,
                "다른 사람이 찾은 관계 종류를 검토한다. 표본에 근거가 없는 것, 방향이 틀린 것, 이미 정한 관계와 뜻이 같은데"
                " 이름만 다른 것을 찾아 고친다. 고친 **목록 전체**를 relations에 낸다(문제가 없으면 그대로).\n"
                'JSON: {"issues":[{"target":"관계 이름","problem":"","suggestion":""}],'
                ' "relations":[{"name":"","from":"","to":"","description":""}]}\n' + _KO,
                f"{material}\n\n개념 목록: {json.dumps(names, ensure_ascii=False)}\n"
                f"이미 정한 관계: {json.dumps(relations, ensure_ascii=False)}\n"
                f"검토할 관계(기준 개념 {anchors}):\n{json.dumps(found, ensure_ascii=False)}")
            issues += [i for i in checked.get("issues") or [] if isinstance(i, dict)]
            reviewed = (_clean_relations(checked["relations"]) if isinstance(checked.get("relations"), list)
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


# --- ② 양식 ---

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
    return problems


def _form_samples(store_name: str, conn, form: dict) -> list[dict]:
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
    """② 양식 하나의 열 대응표 — 제안 → 실제 값 표본과 대조한 검토 → 한 번 고침. 확정은 관리자가."""
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
                " 같은 행의 두 열 사이에 스키마의 어떤 관계가 있는지.\n"
                'JSON: {"columns":{"열 이름":"개념 이름 또는 null"},"relations":[{"from":"열","rel":"관계","to":"열"}]}\n'
                + _KO)
        report("LLM이 열 대응표를 제안하는 중", 2, 4)
        proposal = _ask(db, provider, "되풀이되는 사내 양식 표의 열을 온톨로지에 대응시킨다. " + rule, material)
        report("LLM이 실제 값 표본과 대조해 검토하는 중", 3, 4)
        review = _ask(db, provider,
            "다른 사람이 만든 양식 열 대응표를 **실제 값 표본과 대조해** 검토한다. 값이 그 개념의 이름이 맞는지"
            " (예: '금액' 열을 협력사로 둔 것, 날짜가 든 열을 품목으로 둔 것), 관계 방향이 맞는지.\n"
            'JSON: {"verdict":"ok|revise","issues":[{"column":"","problem":"","suggestion":""}]}\n' + _KO,
            f"{material}\n\n검토할 대응표:\n{json.dumps(proposal, ensure_ascii=False)}")
        revised = proposal
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
                   "relations": [r for r in (mapping.get("relations") or []) if isinstance(r, dict)]}
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
    cols = mapping["columns"]
    nodes, edges = [], []
    for row in _rows_of(markdown, columns):
        evidence = " | ".join(f"{c}: {v}" for c, v in row.items() if v)[:400]
        for col, cls in cols.items():
            value = _space(row.get(col, ""))
            if cls and value:
                nodes.append({"cls": cls, "name": value[:200], "evidence": evidence})
        for r in mapping.get("relations") or []:
            a, b = _space(row.get(r["from"], "")), _space(row.get(r["to"], ""))
            if a and b:
                edges.append({"src_cls": cols[r["from"]], "src": a[:200], "rel": r["rel"],
                              "dst_cls": cols[r["to"]], "dst": b[:200], "evidence": evidence})
    return nodes, edges


# --- ③ 문서 ---

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
        nodes.append({"cls": cls, "name": _space(name)[:200], "evidence": _space(ev)[:400]})
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
        edges.append({"src_cls": key[1], "src": _space(names[0])[:200], "rel": key[0],
                      "dst_cls": key[2], "dst": _space(names[1])[:200], "evidence": _space(ev)[:400]})
    return nodes, edges, dropped, unanchored


def _sampled(path: str) -> bool:
    return int(hashlib.sha1(path.encode()).hexdigest(), 16) % REVIEW_EVERY == 0


def _extract_doc(db, provider, schema: dict, path: str, text: str) -> dict:
    return _ask(db, provider,
        "사내 문서의 **내용**에서 개념과 관계를 뽑는다. 확정한 스키마의 개념·관계 종류만 쓴다. evidence는 문서의"
        " 문장을 **글자 그대로** 옮긴다(요약·번역 금지) — 되도록 개체 이름이 들어 있는 문장으로. outside에는 스키마에"
        " 없는 **개념 종류**(예: 인증·공장)만 적는다 — 개별 회사명·등급 값 같은 개체는 적지 않는다.\n"
        'JSON: {"entities":[{"class":"","name":"","evidence":""}],'
        ' "relations":[{"from":{"class":"","name":""},"rel":"","to":{"class":"","name":""},"evidence":""}],'
        ' "outside":[{"name":"","note":""}]}\n' + _KO,
        f"스키마: {json.dumps({k: schema[k] for k in ('classes', 'relations')}, ensure_ascii=False)}\n\n"
        f"문서 {path}:\n{text}")


def _review_doc(db, provider, schema: dict, text: str, nodes: list[dict], edges: list[dict]) -> dict[int, str]:
    """표본 검토 — 근거 문장이 그 관계·개체를 실제로 말하는지. 틀린 항목의 번호(개체 먼저, 관계 이어서)와 이유."""
    items = ([{"i": i, "entity": n} for i, n in enumerate(nodes)]
             + [{"i": len(nodes) + i, "relation": e} for i, e in enumerate(edges)])
    if not items:
        return {}
    out = _ask(db, provider,
        "다른 사람이 문서에서 뽑은 개념·관계를 검토한다. 항목마다 근거 문장이 그것을 실제로 말하는지, 개념 종류가"
        " 맞는지 본다. 틀린 항목의 i만 적는다.\n"
        'JSON: {"wrong":[{"i":0,"problem":""}]}\n' + _KO,
        f"스키마: {json.dumps({k: schema[k] for k in ('classes', 'relations')}, ensure_ascii=False)}\n\n"
        f"문서:\n{text}\n\n항목:\n{json.dumps(items, ensure_ascii=False)}")
    return {int(w["i"]): str(w.get("problem", ""))[:200] for w in out.get("wrong") or []
            if isinstance(w, dict) and str(w.get("i", "")).isdigit()}


def _write(conn, path: str, nodes: list[dict], edges: list[dict], origin: str) -> None:
    """한 문서의 내용 그래프를 갈아 끼운다. 항목에 origin이 있으면 그것(양식 규칙과 LLM이 한 문서에 섞인다)."""
    conn.execute("DELETE FROM sem_nodes WHERE path = ?", (path,))
    conn.execute("DELETE FROM sem_edges WHERE path = ?", (path,))
    conn.executemany("INSERT INTO sem_nodes (path, cls, name, evidence, origin) VALUES (?, ?, ?, ?, ?)",
                     [(path, n["cls"], n["name"], n["evidence"], n.get("origin", origin)) for n in nodes])
    conn.executemany(
        "INSERT INTO sem_edges (path, src_cls, src, rel, dst_cls, dst, evidence, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(path, e["src_cls"], e["src"], e["rel"], e["dst_cls"], e["dst"], e["evidence"], e.get("origin", origin))
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
        form_of: dict[str, list[str]] = {}
        for r in conn.execute("SELECT DISTINCT path, name FROM nodes WHERE kind = 'table'"):
            if r["name"] in confirmed_forms:
                form_of.setdefault(r["path"], []).append(r["name"])
        known = {r["path"]: r["fp"] for r in conn.execute("SELECT path, fp FROM sem_docs")}
        paths = _paths(conn)
        # 지워진 문서의 내용 그래프는 함께 지운다
        for gone in set(known) - set(paths):
            _write(conn, gone, [], [], "llm")
            conn.execute("DELETE FROM sem_docs WHERE path = ?", (gone,))
        conn.commit()

        stats = {"analyzed": 0, "form_docs": 0, "failed": 0, "reviewed": 0, "dropped": 0}
        outside: dict = _get(conn, "outside", {})
        remaining = 0
        for path in paths:
            fms = sorted(form_of.get(path, []))
            body = conn.execute("SELECT body FROM docs WHERE path = ?", (path,)).fetchone()["body"] or ""
            fp = _digest([hashlib.sha1(body.encode()).hexdigest(), schema["version"],
                          [confirmed_forms[f]["version"] for f in fms]])
            if known.get(path) == fp:
                continue
            if time.monotonic() - started > budget_seconds or should_stop():
                remaining += 1
                continue
            try:
                markdown = _markdown(store_name, path, root)
                form_nodes, form_edges = [], []
                for f in fms:
                    n, e = extract_form(markdown, f.split(" | "), confirmed_forms[f])
                    form_nodes += [{**x, "origin": "form"} for x in n]
                    form_edges += [{**x, "origin": "form"} for x in e]
                rest = strip_forms(markdown, [f.split(" | ") for f in fms]) if fms else markdown
                if fms and len(_compact(rest)) < FORM_REST_CHARS:
                    # 양식 문서(견적서) — 표 밖에 읽을 글이 거의 없다. 규칙으로만.
                    _write(conn, path, form_nodes, form_edges, "form")
                    status, dropped, reviewed = "form", 0, 0
                    stats["form_docs"] += 1
                else:
                    text = rest[:DOC_CHARS]
                    data = _extract_doc(db, provider, schema, path, text)
                    nodes, edges, dropped, unanchored = check_extraction(data, schema, text)
                    reviewed = 0
                    # 표본이거나, 이름이 근거에 없는 항목이 있거나(남겨 둔 대신 판정을 받는다), 버린 것이 더 많으면
                    if _sampled(path) or unanchored or (dropped and dropped >= (len(nodes) + len(edges))):
                        wrong = _review_doc(db, provider, schema, text, nodes, edges)
                        reviewed = 1
                        state["review_items"] = state.get("review_items", 0) + len(nodes) + len(edges)
                        state["review_wrong"] = state.get("review_wrong", 0) + len(wrong)
                        first = len(nodes)   # 관계 번호는 개체 뒤에 이어진다 — 거르기 전의 개수로
                        # 무엇을 왜 틀렸다고 했는지 남긴다 — 멈췄을 때 관리자가 스키마를 고칠지, 그냥 다시 시작할지 고를 근거다.
                        notes = state.get("review_notes", [])
                        for i, problem in wrong.items():
                            item = nodes[i] if i < first else (edges[i - first] if i - first < len(edges) else None)
                            if item:
                                label = (f'{item["cls"]}:{item["name"]}' if "cls" in item
                                         else f'{item["src"]} —{item["rel"]}→ {item["dst"]}')
                                notes.append({"path": path, "item": label, "evidence": item["evidence"][:160],
                                              "problem": problem})
                        state["review_notes"] = notes[-30:]
                        nodes = [n for i, n in enumerate(nodes) if i not in wrong]
                        edges = [e for i, e in enumerate(edges) if first + i not in wrong]
                        stats["reviewed"] += 1
                    for o in data.get("outside") or []:
                        if isinstance(o, dict) and o.get("name"):
                            name = str(o["name"])[:60]
                            outside[name] = outside.get(name, 0) + 1
                    _write(conn, path, form_nodes + nodes, form_edges + edges, "llm")
                    status = "ok"
                    stats["dropped"] += dropped
                conn.execute("INSERT OR REPLACE INTO sem_docs (path, fp, status, error, truncated, dropped, reviewed,"
                             " analyzed_at) VALUES (?, ?, ?, NULL, ?, ?, ?, ?)",
                             (path, fp, status, int(status == "ok" and len(rest) > DOC_CHARS), dropped, reviewed,
                              time.time()))
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
        outside = dict(sorted(outside.items(), key=lambda kv: -kv[1])[:100])
        _put(conn, "outside", outside)
        return {**stats, "remaining": remaining, "done": remaining == 0 and not state.get("paused"),
                "paused": state.get("paused")}
    finally:
        conn.close()


def resume(store_name: str) -> None:
    """멈춘 분석을 다시 — 표본 오류 집계도 비운다(고친 스키마·대응표로 새로 잰다)."""
    conn = _conn(store_name)
    try:
        _put(conn, "state", {"paused": None, "review_items": 0, "review_wrong": 0, "review_notes": []})
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
            put(done=False, progress=text, step=step, steps=steps)

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

_NORM_RE = re.compile(r"\(주\)|㈜|주식회사|\(유\)|유한회사|[\s\.\,\-_·()\[\]{}'\"]")


def normalize(name: str) -> str:
    return _NORM_RE.sub("", name).lower()


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


def propose_merges(db: Session, store_name: str, report=_quiet) -> dict:
    """④ 같은 개체 묶기. 정규화가 같은 이름은 바로 묶는다(기계적이라 검토하지 않는다). 그 밖의 애매한 것은 LLM이
    묶음을 제안하고 **묶음마다** 검토한다 — 합치기는 되돌리기 어렵다(되돌리기는 별칭 삭제로 할 수 있다)."""
    provider = provider_for(db, store_name)
    conn = _conn(store_name)
    try:
        aliases = canonical_map(conn)
        applied, rejected = [], []
        names_by_cls: dict[str, dict[str, int]] = {}
        for r in conn.execute("SELECT cls, name, COUNT(DISTINCT path) AS n FROM sem_nodes GROUP BY cls, name"):
            names_by_cls.setdefault(r["cls"], {})[r["name"]] = r["n"]

        def link(cls: str, alias: str, canonical: str, reason: str) -> None:
            if alias != canonical and (cls, alias) not in aliases:
                conn.execute("INSERT OR REPLACE INTO sem_aliases (cls, alias, canonical, reason) VALUES (?, ?, ?, ?)",
                             (cls, alias, canonical, reason))
                aliases[(cls, alias)] = canonical
                applied.append({"cls": cls, "alias": alias, "canonical": canonical, "reason": reason})

        for n, (cls, names) in enumerate(names_by_cls.items(), 1):
            report(f"'{cls}' 이름 묶기·검토 중", n, len(names_by_cls))
            groups: dict[str, list[str]] = {}
            for name in names:
                groups.setdefault(normalize(name), []).append(name)
            for same in groups.values():
                if len(same) > 1:
                    head = max(same, key=lambda n: (names[n], -len(n)))
                    for other in same:
                        link(cls, other, head, "이름 정규화가 같다")
            # 애매한 것 — 아직 묶이지 않은 이름들(많이 나오는 것부터)
            rest = sorted((n for n in names if (cls, n) not in aliases), key=lambda n: -names[n])[:MERGE_NAMES]
            if len(rest) < 2:
                continue
            proposal = _ask(db, provider,
                f"'{cls}' 개념의 이름 목록에서 **같은 대상**을 가리키는 이름들을 묶는다(약칭·영문·한글 표기·오타)."
                " 확실한 것만. 비슷하지만 다른 대상(다른 회사·다른 품목)은 묶지 않는다.\n"
                'JSON: {"groups":[{"canonical":"대표 이름","names":["다른 이름"],"reason":""}]}\n' + _KO,
                json.dumps(rest, ensure_ascii=False))
            for g in proposal.get("groups") or []:
                if not isinstance(g, dict):
                    continue
                canonical = str(g.get("canonical", ""))
                others = [str(n) for n in g.get("names") or [] if str(n) in names and str(n) != canonical]
                if canonical not in names or not others:
                    continue
                verdict = _ask(db, provider,
                    f"다른 사람이 '{cls}' 개념의 이름들을 같은 대상으로 묶자고 했다. 정말 같은 대상인지 검토한다."
                    " 확신이 없으면 거절한다.\n"
                    'JSON: {"same":true,"reason":""}\n' + _KO,
                    json.dumps({"canonical": canonical, "names": others, "reason": g.get("reason", "")},
                               ensure_ascii=False))
                if verdict.get("same") is True:
                    for other in others:
                        link(cls, other, canonical, str(g.get("reason", ""))[:200])
                else:
                    rejected.append({"cls": cls, "canonical": canonical, "names": others,
                                     "reason": str(verdict.get("reason", ""))[:200]})
        conn.commit()
        result = {"applied": applied, "rejected": rejected, "at": _now()}
        _put(conn, "last_merge", result)
        return result
    finally:
        conn.close()


def remove_alias(store_name: str, cls: str, alias: str) -> None:
    conn = _conn(store_name)
    try:
        conn.execute("DELETE FROM sem_aliases WHERE cls = ? AND alias = ?", (cls, alias))
        conn.commit()
    finally:
        conn.close()


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
                       "stop_rate": STOP_ERROR_RATE, "notes": state.get("review_notes", [])},
            "last_run": _get(conn, "last_run"), "outside": _get(conn, "outside", {}),
            "task": {**(_get(conn, "task") or {}), "running": task_running(store_name)},
            "last_merge": _get(conn, "last_merge"),
            "aliases": [dict(r) for r in conn.execute("SELECT cls, alias, canonical, reason FROM sem_aliases ORDER BY cls, canonical")],
            "failed": [dict(r) for r in conn.execute(
                "SELECT path, error FROM sem_docs WHERE status = 'failed' ORDER BY path LIMIT 20")],
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
        for r in conn.execute("SELECT cls, name, path FROM sem_nodes"):
            key = (r["cls"], _canon(aliases, r["cls"], r["name"]))
            if cls and key[0] != cls:
                continue
            g = groups.setdefault(key, {"cls": key[0], "name": key[1], "paths": set(), "names": set()})
            g["paths"].add(r["path"])
            g["names"].add(r["name"])
        needle = q.strip().lower()
        out = [{"cls": g["cls"], "name": g["name"], "documents": len(g["paths"]),
                "aliases": sorted(g["names"] - {g["name"]})}
               for g in groups.values()
               if not needle or any(needle in n.lower() for n in g["names"])]
        return sorted(out, key=lambda g: (-g["documents"], g["name"]))[:limit]
    finally:
        conn.close()


def neighbors(store_name: str, cls: str, name: str, limit: int = 60) -> dict:
    """한 개체의 관계 — 관계·상대마다 묶고, 근거 문장과 문서를 함께(원문으로 되짚을 수 있게)."""
    conn = _conn(store_name)
    try:
        aliases = canonical_map(conn)
        names = {name} | {a for (c, a), canon in aliases.items() if c == cls and canon == name}
        marks = ",".join("?" * len(names))
        rows = list(conn.execute(
            f"SELECT * FROM sem_edges WHERE (src_cls = ? AND src IN ({marks})) OR (dst_cls = ? AND dst IN ({marks}))",
            (cls, *names, cls, *names)))
        grouped: dict[tuple, dict] = {}
        for r in rows:
            outgoing = r["src_cls"] == cls and r["src"] in names
            other_cls, other = (r["dst_cls"], r["dst"]) if outgoing else (r["src_cls"], r["src"])
            other = _canon(aliases, other_cls, other)
            key = (r["rel"], "out" if outgoing else "in", other_cls, other)
            g = grouped.setdefault(key, {"rel": r["rel"], "direction": key[1], "cls": other_cls, "name": other,
                                         "evidence": []})
            if len(g["evidence"]) < 5:
                g["evidence"].append({"path": r["path"], "text": r["evidence"], "origin": r["origin"]})
        mentions = [dict(r) for r in conn.execute(
            f"SELECT path, evidence, origin FROM sem_nodes WHERE cls = ? AND name IN ({marks}) LIMIT 10", (cls, *names))]
        relations = sorted(grouped.values(), key=lambda g: -len(g["evidence"]))[:limit]
        return {"cls": cls, "name": name, "relations": relations, "mentions": mentions}
    finally:
        conn.close()
