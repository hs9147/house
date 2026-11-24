"""전환 현황 — 저장소마다 **정보 조회 → 내용 분석 → 워크플로 연결**이 어디까지 왔고 다음에 무엇을 할지.

예전 전환 현황은 형식 그래프(색인 → 절·용어·표)만 셌다. 그런데 일은 그 뒤로 이어진다:

  정보 조회     색인 → 본문 추출 → 형식 구조(절·용어·표) — services/ontology_status
  내용 분석     ① 스키마 → ②-1 양식 · ②-2 템플릿 · ②-3 경로 틀(규칙) → ③ 분석(LLM) — services/semantic
  워크플로 연결  부서 워크플로의 낱말·단계가 저장소의 문서·용어·조문에 닿는 선 — services/workflowlinks

한 저장소가 어느 단계에서 멈췄는지가 한 줄에 보여야 다음 할 일이 보인다. 그래서 저장소 × 단계로 모으고, 저장소마다
**다음 할 일 하나**를 정해 준다(필수 단계만 — ②-1·②-2는 해 두면 LLM을 덜 쓰는 선택이라 막지 않는다).

**아무것도 바꾸지 않는다** — 색인 DB와 플랫폼 DB를 읽기만 한다. 비싼 계산(템플릿 묶기)은 하지 않고 지난 결과만 읽는다.
"""
from __future__ import annotations

from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import WorkflowLink
from . import ontology_status, semantic


def _semantic_part(store_name: str) -> dict:
    conn = semantic._conn(store_name)
    try:
        schema = semantic._get(conn, "schema")    # 없으면 None(semantic._schema()는 없으면 예외다)
        forms_saved = semantic._get(conn, "forms", {}) or {}
        tpl_saved = semantic._get(conn, "templates", {}) or {}
        tpl_cached = semantic._get(conn, "template_groups") or {}
        paths = semantic._get(conn, "paths", {}) or {}
        confirmed = paths.get("confirmed")
        stale = bool(confirmed and confirmed.get("schema_version") != (schema or {}).get("version"))
        analyzed = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM sem_docs GROUP BY status")}
        state = semantic._get(conn, "state", {}) or {}
        nodes = {r["origin"]: r["n"] for r in conn.execute("SELECT origin, COUNT(*) AS n FROM sem_nodes GROUP BY origin")}
        out = {
            "schema": ({"version": schema.get("version", ""), "classes": len(schema.get("classes") or []),
                        "relations": len(schema.get("relations") or []),
                        "layered": all("layer" in c for c in schema.get("classes") or [])} if schema else None),
            "forms": {"total": len(semantic._form_tables(conn)),
                      "confirmed": sum(1 for f in forms_saved.values() if f.get("confirmed"))},
            # 템플릿 묶기는 문서마다 앞부분을 읽어야 해서 비싸다 — 내용 분석 화면이 계산해 둔 것만 센다(없으면 None)
            "templates": {"total": len(tpl_cached.get("groups") or {}) if tpl_cached else None,
                          "confirmed": sum(1 for t in tpl_saved.values() if t.get("confirmed") and not t.get("suspended"))},
            "paths": {"candidates": len(semantic._path_candidates(store_name)),
                      "confirmed": len((confirmed or {}).get("slots") or []), "stale": stale,
                      "applied": semantic._get(conn, "paths_applied"),
                      "mismatches": semantic._path_mismatches(conn, None if stale else confirmed)["documents"]},
            "documents": conn.execute("SELECT COUNT(*) FROM docs WHERE body IS NOT NULL").fetchone()[0],
            "analyzed": analyzed,
            "paused": state.get("paused"),
            "running": semantic.running(store_name),
            "nodes": nodes,
            "edges": conn.execute("SELECT COUNT(*) FROM sem_edges").fetchone()[0],
            "aliases": conn.execute("SELECT COUNT(*) FROM sem_aliases").fetchone()[0],
            # 문서 수 — 내용에서 개체·관계가 나온 문서, 경로 틀로 개체가 달린 문서
            "content_docs": conn.execute("SELECT COUNT(DISTINCT path) FROM sem_nodes WHERE origin != 'path'").fetchone()[0],
            "path_docs": conn.execute("SELECT COUNT(DISTINCT path) FROM sem_nodes WHERE origin = 'path'").fetchone()[0],
        }
    finally:
        conn.close()
    return out


def _workflow_part(db: Session, names: list[str]) -> dict[str, dict]:
    out = {n: {"links": 0, "workflows": 0, "documents": 0, "by_source": {}, "pending": 0} for n in names}
    if not names:
        return out
    wf: dict[str, set] = {n: set() for n in names}
    docs: dict[str, set] = {n: set() for n in names}
    src: dict[str, Counter] = {n: Counter() for n in names}
    for r in db.execute(select(WorkflowLink.store, WorkflowLink.workflow_id, WorkflowLink.path, WorkflowLink.source,
                               WorkflowLink.confirmed).where(WorkflowLink.store.in_(names))):
        o = out[r.store]
        o["links"] += 1
        wf[r.store].add(r.workflow_id)
        if r.path:
            docs[r.store].add(r.path)
        src[r.store][r.source] += 1
        o["pending"] += int(r.source == "llm" and not r.confirmed)
    for n in names:
        out[n].update(workflows=len(wf[n]), documents=len(docs[n]), by_source=dict(src[n]))
    return out


def next_step(p: dict) -> dict:
    """저장소의 다음 할 일 하나 — 필수 단계만, 일하는 순서로. tab은 그 일을 하는 화면(온톨로지의 탭)."""
    f, s, w = p["formal"], p["semantic"], p["workflow"]
    if not f["indexed"]:
        return {"stage": "정보 조회", "text": "색인된 문서가 없습니다 — 저장소 경로와 색인 주기 작업을 확인하세요", "tab": ""}
    if s is None:
        return {"stage": "내용 분석", "text": "내용 분석 상태를 읽지 못했습니다", "tab": "semantic"}
    if not s["schema"]:
        return {"stage": "내용 분석", "text": "① 스키마를 제안·확정하세요", "tab": "semantic"}
    if s["paths"]["stale"]:
        return {"stage": "내용 분석", "text": "②-3 경로 틀을 다시 확정하세요(스키마 판이 바뀌었습니다)", "tab": "semantic"}
    if s["paths"]["candidates"] and not s["paths"]["confirmed"]:
        return {"stage": "내용 분석", "text": f"②-3 경로 틀 — 식별자 칸 {s['paths']['candidates']}개를 개념에 대응시키세요",
                "tab": "semantic"}
    if s["paused"]:
        return {"stage": "내용 분석", "text": "③ 분석이 멈췄습니다 — 표본 검토 메모를 보고 고친 뒤 다시 시작하세요", "tab": "semantic"}
    done = sum(s["analyzed"].values())
    if done < s["documents"]:
        return {"stage": "내용 분석", "text": f"③ 분석을 {'진행 중입니다' if s['running'] else '이어서 하세요'}"
                                             f" ({done:,}/{s['documents']:,})", "tab": "semantic"}
    if not w["links"]:
        return {"stage": "워크플로 연결", "text": "이 저장소를 쓰는 워크플로를 연결하세요", "tab": "workflows"}
    if w["pending"]:
        return {"stage": "워크플로 연결", "text": f"LLM이 제안한 연결 {w['pending']}개를 확인하세요", "tab": "workflows"}
    return {"stage": "", "text": "다음 할 일 없음", "tab": ""}


def progress(db: Session, stores) -> dict:
    """저장소마다 세 단계와 다음 할 일, 합계. 한 저장소의 실패가 나머지를 막지 않는다."""
    names = [s.name for s in stores]
    workflow = _workflow_part(db, names)
    rows, errors = [], []
    for name in names:
        try:
            formal = ontology_status.store_summary(name)
        except Exception as e:  # noqa: BLE001
            errors.append({"store": name, "stage": "정보 조회", "error": str(e)[:200]})
            continue
        try:
            sem = _semantic_part(name)
        except Exception as e:  # noqa: BLE001
            errors.append({"store": name, "stage": "내용 분석", "error": str(e)[:200]})
            sem = None
        row = {"store": name,
               "formal": {k: formal[k] for k in ("indexed", "extracted", "extract_failed", "with_nodes", "no_nodes",
                                                 "nodes", "edges")},
               "semantic": sem, "workflow": workflow[name]}
        row["next"] = next_step(row)
        rows.append(row)

    def total(fn) -> int:
        return sum(fn(r) for r in rows)

    sem_rows = [r for r in rows if r["semantic"]]
    totals = {
        "stores": len(rows),
        "indexed": total(lambda r: r["formal"]["indexed"]),
        "extracted": total(lambda r: r["formal"]["extracted"]),
        "with_nodes": total(lambda r: r["formal"]["with_nodes"]),
        "analyzed": sum(sum(r["semantic"]["analyzed"].values()) for r in sem_rows),
        "content_docs": sum(r["semantic"]["content_docs"] for r in sem_rows),
        "path_docs": sum(r["semantic"]["path_docs"] for r in sem_rows),
        "schemas": sum(1 for r in sem_rows if r["semantic"]["schema"]),
        "paths_confirmed": sum(1 for r in sem_rows if r["semantic"]["paths"]["confirmed"]),
        "paths_possible": sum(1 for r in sem_rows if r["semantic"]["paths"]["candidates"]),
        "workflow_docs": total(lambda r: r["workflow"]["documents"]),
        "workflow_links": total(lambda r: r["workflow"]["links"]),
        "linked_workflows": db.execute(select(func.count(func.distinct(WorkflowLink.workflow_id)))
                                       .where(WorkflowLink.store.in_(names))).scalar_one() if names else 0,
    }
    return {"stores": rows, "totals": totals, "errors": errors}
