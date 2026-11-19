"""온톨로지 운영 기준 점검 — 저장소마다 「운영에 쓸 만한가」를 기준과 수치로 판정한다(정상 ok · 주의 warn · 위반 fail).

전환 현황(ontology_status)과 내용 분석 상태(semantic.status)는 수치를 보여 줄 뿐, 그 수치가 괜찮은지는 사람이 읽어야
했다. 운영에서 실제로 보인 문제가 기준이 됐다(실측 2026-11): contracts 색인 실패 21%, 층위 없이 확정한 스키마, 사람 정답
0개라 묶기 기준을 배우지 못함. 기준마다 **조치**를 함께 준다 — 어디를 눌러 고치는지.

LLM을 쓰지 않는다 — 전부 색인 DB를 센 사실이다. 화면(내용 분석 탭)과 운영 MCP(ops__ontology_checks)가 같은 함수를 쓴다.
"""
from __future__ import annotations

from . import docsearch, ontology_status, semantic

# 기준 — (정상 문턱, 주의 문턱). 비율은 0~1.
INDEX_FAILURE = (0.05, 0.15)       # 색인 실패율 이하
STRUCTURE = (0.5, 0.3)             # 구조(절·용어·표)가 생긴 문서 비율 이상
ANALYZED = (0.95, 0.5)             # 내용 분석을 마친 문서 비율 이상
ANALYSIS_FAILED = (0.02, 0.05)     # 내용 분석 실패율 이하
REVIEW_ERROR = (0.10, semantic.STOP_ERROR_RATE)   # 표본 검토 오류율 이하(주의 문턱 = 멈춤 기준)
OBSERVED = (0.9, 0.5)              # 시점에 매인 관계 중 관측한 날이 있는 비율 이상
VERSIONED = (0.99, 0.8)            # 판 정보(시행일·개정 계열)를 만든 문서 비율 이상
GOLD_MIN = 20                      # 사람 정답 — semantic_tune.MIN_LABELS와 같다
GARBLED_DOCS = (0.01, 0.05)        # 본문이 깨진 문서 비율 이하(실측 contracts의 .doc — 코드 페이지 문제)


def _level(value: float, good: float, warn: float, higher_is_better: bool = True) -> str:
    if higher_is_better:
        return "ok" if value >= good else "warn" if value >= warn else "fail"
    return "ok" if value <= good else "warn" if value <= warn else "fail"


def _check(key: str, label: str, level: str, value: str, target: str, action: str = "") -> dict:
    return {"key": key, "label": label, "level": level, "value": value, "target": target,
            "action": action if level != "ok" else ""}


def _pct(x: float) -> str:
    return f"{x:.0%}" if x >= 0.1 or x == 0 else f"{x:.1%}"


def checks(store_name: str) -> dict:
    """저장소 하나의 운영 기준 점검."""
    out: list[dict] = []
    summary = ontology_status.store_summary(store_name)
    total, extracted = summary["indexed"], summary["extracted"]
    if not total:
        return {"store": store_name, "checks": [_check("index", "색인", "fail", "문서 0건", "문서가 색인돼 있다",
                                                       "저장소 경로와 색인 주기 작업(doc_index)을 확인하세요")],
                "summary": {"ok": 0, "warn": 0, "fail": 1}}
    failed = summary["extract_failed"] / total
    reasons = summary.get("failure_reasons") or {}
    top = ", ".join(f"{r}({n})" for r, n in (list(reasons.items()) if isinstance(reasons, dict) else [])[:3])
    out.append(_check("index_failure", "색인 실패율", _level(failed, *INDEX_FAILURE, higher_is_better=False),
                      f"{_pct(failed)} ({summary['extract_failed']:,}/{total:,})",
                      f"≤ {_pct(INDEX_FAILURE[0])}",
                      f"전환 현황에서 실패 이유를 보고 추출기(스캔 PDF OCR·LibreOffice)를 갖추세요 — {top}" if top else
                      "전환 현황에서 실패 이유를 보세요"))
    structure = summary["with_nodes"] / extracted if extracted else 0.0
    g = docsearch.garbled_docs(store_name, 0)
    broken = g["garbled"] / g["total"] if g["total"] else 0.0
    out.append(_check("garbled", "깨진 본문(한글이 ?로)", _level(broken, *GARBLED_DOCS, higher_is_better=False),
                      f"{_pct(broken)} ({g['garbled']:,}/{g['total']:,})"
                      + (f" — {', '.join(f'{k} {v}' for k, v in g['by_suffix'].items())}" if g["by_suffix"] else ""),
                      f"≤ {_pct(GARBLED_DOCS[0])}",
                      "추출을 고친 형식을 다시 추출하세요(ops__ontology_reextract) — 깨진 문서는 내용 분석이 건너뜁니다"))
    out.append(_check("structure", "구조 전환율(절·용어·표)", _level(structure, *STRUCTURE), f"{_pct(structure)}",
                      f"≥ {_pct(STRUCTURE[0])}", "제목·표가 없는 평문이 많습니다 — 내용 분석(LLM)이 그 관계를 맡습니다"))

    conn = docsearch._connect(store_name)
    try:
        with_body = conn.execute("SELECT COUNT(*) AS n FROM docs WHERE body IS NOT NULL").fetchone()["n"]
        versioned = conn.execute("SELECT COUNT(*) AS n FROM doc_meta WHERE path IN "
                                 "(SELECT path FROM docs WHERE body IS NOT NULL)").fetchone()["n"]
    finally:
        conn.close()
    v = versioned / with_body if with_body else 1.0
    out.append(_check("versioned", "판 정보(시행일·개정 계열)", _level(v, *VERSIONED), f"{_pct(v)} ({versioned:,}/{with_body:,})",
                      f"≥ {_pct(VERSIONED[0])}", "내용 분석 ⑥ 재구성으로 형식 그래프의 판 정보를 다시 만드세요"))

    st = semantic.status(store_name)
    schema = st.get("schema")
    if not schema:
        out.append(_check("schema", "내용 분석 스키마", "fail", "확정 안 됨", "층위가 있는 스키마를 확정",
                          "내용 분석 ① 스키마 제안 → 층위 확인 → 확정"))
    else:
        layered = all("layer" in c for c in schema.get("classes") or [])
        temporal = sum(1 for r in schema.get("relations") or [] if r.get("temporal"))
        out.append(_check("schema", "내용 분석 스키마 — 층위", "ok" if layered else "warn",
                          f"판 {schema.get('version', '')[:8]} · 개념 {len(schema.get('classes') or [])} · "
                          f"시점 관계 {temporal}" + ("" if layered else " · 층위 없음"),
                          "개념마다 층위(기준 지식·대상·상태·사건)",
                          "층위를 넣기 전에 확정한 스키마입니다 — ⑥ 재구성(추출 결과만) 뒤 ① 스키마를 다시 제안·확정하세요"))
    docs = st["documents"] or 0
    done = sum(st["analyzed"].values())
    if schema and docs:
        a = done / docs
        out.append(_check("analyzed", "내용 분석 진행", _level(a, *ANALYZED), f"{_pct(a)} ({done:,}/{docs:,})",
                          f"≥ {_pct(ANALYZED[0])}", "③ 분석을 시작(또는 이어서)하세요"))
        bad = (st["analyzed"].get("failed", 0) / done) if done else 0.0
        out.append(_check("analysis_failed", "내용 분석 실패율", _level(bad, *ANALYSIS_FAILED, higher_is_better=False),
                          f"{_pct(bad)} ({st['analyzed'].get('failed', 0):,})", f"≤ {_pct(ANALYSIS_FAILED[0])}",
                          "③ 분석의 실패 목록에서 사유를 보세요(LLM 연결·문서 형식)"))
    if st.get("paused"):
        out.append(_check("paused", "분석 멈춤", "fail", st["paused"][:120], "멈춤 없음",
                          "표본 검토 메모를 보고 스키마·대응표를 고친 뒤 다시 시작하세요"))
    items = st["review"]["items"]
    if schema and items:
        rate = st["review"]["wrong"] / items
        level = _level(rate, *REVIEW_ERROR, higher_is_better=False) if items >= semantic.STOP_MIN_ITEMS else "warn"
        out.append(_check("review", "표본 검토 오류율", level, f"{_pct(rate)} ({st['review']['wrong']}/{items})",
                          f"≤ {_pct(REVIEW_ERROR[0])} (멈춤 {_pct(REVIEW_ERROR[1])})",
                          "표본이 적습니다 — 분석을 더 돌리세요" if items < semantic.STOP_MIN_ITEMS else
                          "틀린 항목의 메모를 보고 스키마의 개념·관계 설명을 고치세요"))
    if schema:
        rels = semantic.temporal_rels(schema)
        if rels:
            sconn = semantic._conn(store_name)
            try:
                marks = ",".join("?" * len(rels))
                row = sconn.execute(f"SELECT COUNT(*) AS n, SUM(observed_at != '') AS seen FROM sem_edges WHERE rel IN ({marks})",
                                    tuple(rels)).fetchone()
            finally:
                sconn.close()
            if row["n"]:
                o = (row["seen"] or 0) / row["n"]
                out.append(_check("observed", "시점 기록(상태·사건 관계)", _level(o, *OBSERVED),
                                  f"{_pct(o)} ({row['seen'] or 0:,}/{row['n']:,})", f"≥ {_pct(OBSERVED[0])}",
                                  "시점이 없던 때의 추출입니다 — ⑥ 재구성(추출 결과만) 뒤 ③ 분석을 다시 돌리세요"))
        gold = sum(m["labeled"] for m in (st.get("gold") or {}).values())
        out.append(_check("gold", "사람 정답(묶기 기준 배우기)", "ok" if gold >= GOLD_MIN else "warn", f"{gold}개",
                          f"≥ {GOLD_MIN}개", f"⑤ 정답 확인에서 짝·개체를 {GOLD_MIN - gold}개 더 확인하세요 — 그래야 하루 한 번"
                          " 묶기 기준을 다시 배웁니다" if gold < GOLD_MIN else ""))
    conflicts = ontology_status.definition_conflicts(store_name)
    out.append(_check("conflicts", "정의가 갈리는 용어", "ok" if not conflicts else "warn", f"{len(conflicts)}개", "0개",
                      "⑦ 품질 점검에서 표준 용어를 정하세요(같은 용어를 지금 판의 문서들이 다르게 정의합니다): "
                      + ", ".join(c["term"] for c in conflicts[:5])))
    counts = {k: sum(1 for c in out if c["level"] == k) for k in ("ok", "warn", "fail")}
    return {"store": store_name, "checks": out, "summary": counts}


def all_checks() -> list[dict]:
    """색인된 저장소 전부 — 문서가 없는 저장소는 요약만."""
    from . import storage as storage_service  # noqa: PLC0415

    out = []
    for store in storage_service.indexed_stores():
        try:
            out.append(checks(store.name))
        except Exception as e:  # noqa: BLE001 — 한 저장소의 실패가 나머지를 막지 않게
            out.append({"store": store.name, "error": str(e)[:300], "checks": [], "summary": {}})
    return out
