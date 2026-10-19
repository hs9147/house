"""워크플로 ↔ 저장소 온톨로지 연결(models.WorkflowLink).

문서 온톨로지는 문서에서만 나온다(services/ontology — 정의문·조문·표 머리글). 워크플로가 다루는
낱말(계약·협력사·승인)은 대개 정의 없이 본문에 쓰이기만 해서 노드가 없다. 그래서 온톨로지를
워크플로에 맞춰 바꾸지 않고(바꾸면 워크플로를 고칠 때마다 저장소 전체를 재색인해야 한다), 이
층에서 워크플로 쪽 선을 긋는다. 근거가 확실한 것부터 셋:

  A  설계·실행 — 단계가 쓰는 저장소(spec), 단계가 실제로 읽은 문서(실행 기록)
  B  이름 — 대상·상태 ↔ 같은 이름의 용어·조문·표(name), 그 낱말이 많이 나오는 문서(index),
     규칙 속 「제N조」·「문서명」 ↔ 그 조문·문서(name)
  C  LLM 제안 — B가 놓친 짝(동의어·다른 표현)을 **후보 목록 안에서만** 고른다. 사람이 확인해야 쓰인다.

A·B는 결정론이라 언제든 통째로 다시 계산한다(워크플로 저장·실행 끝·저장소 재색인 뒤). C는
지우지 않는다 — 사람이 확인한 판단이다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Workflow, WorkflowLink, WorkflowRun
from . import docsearch, storage

# 대상·상태 하나가 한 저장소에서 잇는 노드 수 — "계약" 같은 흔한 낱말이 노드 수백 개와 이어지지 않게.
MAX_NODES = 8
# 그 낱말이 많이 나오는 문서 몇 건까지 — 문서 언급은 대표만 남긴다.
MAX_DOCS = 5
# 이보다 많은 문서에 나오면 흔한 낱말이라고 적는다(대표 문서만 잇는 까닭을 화면에 밝힌다).
COMMON_DOCS = 100
# 실행 기록은 최근 몇 건까지 본다.
RECENT_RUNS = 20
MIN_NAME = 2
_ARTICLE_RE = re.compile(r"제\s*(\d{1,3})\s*조")
_DOCREF_RE = re.compile(r"[「〈]([^」〉\n]{2,60})[」〉]")
_RECOMPUTED = ("spec", "run", "name", "index")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def stores_of(db: Session, workflow: Workflow) -> list[storage.Store]:
    """이 워크플로가 쓰는 저장소 — 설계에 적힌 것. 하나도 없으면 그 부서의 저장소 전부."""
    own = [s for s in storage.stores(db) if s.organization_id == workflow.organization_id]
    named = {str(n.get("store")) for n in _nodes(workflow) if n.get("store")}
    used = [s for s in own if s.name in named]
    return used or own


def _nodes(workflow: Workflow) -> list[dict]:
    return [n for n in (workflow.spec or {}).get("nodes", []) if isinstance(n, dict)]


def _rows(workflow: Workflow, key: str) -> list[dict]:
    found = (workflow.extracted or {}).get(key)
    return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []


def subjects(workflow: Workflow) -> list[tuple[str, str]]:
    """워크플로 쪽 낱말 — (종류, 이름). 대상·상태는 이름, 규칙은 문장."""
    out: list[tuple[str, str]] = []
    for kind, key, field in (("entity", "entities", "name"), ("state", "states", "name"),
                             ("constraint", "constraints", "text")):
        for row in _rows(workflow, key):
            name = str(row.get(field) or "").strip()
            if name and (kind, name) not in out:
                out.append((kind, name))
    return out


def _link(workflow: Workflow, subject_kind: str, subject: str, rel: str, store: str,
          target_kind: str, target: str, source: str, path: str = "", detail: str = "",
          confirmed: bool = True) -> WorkflowLink:
    return WorkflowLink(workflow_id=workflow.id, subject_kind=subject_kind, subject=subject[:400],
                        rel=rel, store=store, target_kind=target_kind, target=target[:400],
                        path=path[:1024], detail=detail[:400], source=source, confirmed=confirmed)


def _from_design(workflow: Workflow) -> list[WorkflowLink]:
    out = []
    for node in _nodes(workflow):
        if node.get("store"):
            out.append(_link(workflow, "step", str(node.get("id") or ""), "uses", str(node["store"]),
                             "store", str(node["store"]), "spec",
                             detail=str(node.get("type") or "")))
    return out


def _from_runs(db: Session, workflow: Workflow, stores: list[storage.Store]) -> list[WorkflowLink]:
    """단계가 실제로 읽은 문서 — 실행 기록(outputs의 paths). 저장소를 적지 않은 단계는 경로로 찾는다."""
    by_id = {str(n.get("id")): n for n in _nodes(workflow)}
    by_name = {s.name: s for s in stores}
    runs = db.execute(select(WorkflowRun).where(WorkflowRun.workflow_id == workflow.id)
                      .order_by(WorkflowRun.id.desc()).limit(RECENT_RUNS)).scalars()
    seen: set[tuple[str, str, str]] = set()
    out = []
    for run in runs:
        for node_id, result in (run.outputs or {}).items():
            node = by_id.get(str(node_id), {})
            for rel in (result or {}).get("paths") or []:
                store = by_name.get(str(node.get("store") or ""))
                if store is None:
                    store = next((s for s in stores if (s.root / Path(rel)).is_file()), None)
                if store is None or (node_id, store.name, rel) in seen:
                    continue
                seen.add((node_id, store.name, rel))
                out.append(_link(workflow, "step", str(node_id), "read", store.name, "document",
                                 rel, "run", path=rel, detail=str(node.get("type") or "")))
    return out


def _name_matches(store: str, name: str) -> list[dict]:
    """같은 이름의 용어(정확히), 그 낱말이 든 조문 제목, 그 낱말이 든 표 컬럼 — 문서 수가 많은 것부터."""
    key = _norm(name)
    picked = []
    for row in docsearch.node_search(store, q=name, limit=60):
        kind, node_name = row["kind"], row["name"]
        if kind == "term" and _norm(node_name) == key:
            rank = 0
        elif kind == "section" and key in _norm(node_name):
            rank = 1
        elif kind == "table" and any(key in _norm(col) for col in node_name.split("|")):
            rank = 2
        else:
            continue
        picked.append((rank, -row["documents"], row))
    picked.sort(key=lambda t: (t[0], t[1]))
    return [row for _, _, row in picked[:MAX_NODES]]


def _from_names(workflow: Workflow, stores: list[storage.Store]) -> list[WorkflowLink]:
    out = []
    for kind, name in subjects(workflow):
        if kind == "constraint":
            out += _grounds(workflow, name, stores)
            continue
        if len(_norm(name)) < MIN_NAME:
            continue
        for store in stores:
            for row in _name_matches(store.name, name):
                out.append(_link(workflow, kind, name, "matches", store.name, row["kind"], row["name"],
                                 "name", detail=f"문서 {row['documents']}건"))
            found = docsearch.mentions(store.name, name, MAX_DOCS)
            for hit in found["hits"]:
                common = f" · 흔한 낱말(문서 {found['total']}건 중 많이 나오는 곳)" if found["total"] > COMMON_DOCS else ""
                out.append(_link(workflow, kind, name, "mentions", store.name, "document", hit["path"],
                                 "index", path=hit["path"], detail=f"{hit['count']}번{common}"))
    return out


def _grounds(workflow: Workflow, rule: str, stores: list[storage.Store]) -> list[WorkflowLink]:
    """규칙 문장 속 「제N조」·「문서명」 — 그 조문·문서가 근거다."""
    out = []
    articles = {f"제{n}조" for n in _ARTICLE_RE.findall(rule)}
    docs = {d.strip() for d in _DOCREF_RE.findall(rule)}
    for store in stores:
        for article in sorted(articles):
            for row in docsearch.find_nodes(store.name, "section", article, MAX_NODES):
                if row.get("detail") == article or _norm(row["name"]).startswith(_norm(article)):
                    out.append(_link(workflow, "constraint", rule, "grounded_in", store.name, "section",
                                     row["name"], "name", path=row["path"], detail=article))
        for name in sorted(docs):
            for row in docsearch.find_nodes(store.name, "document", name, MAX_NODES):
                out.append(_link(workflow, "constraint", rule, "grounded_in", store.name, "document",
                                 row["name"], "name", path=row["path"], detail="인용한 문서"))
    return out


def compute(db: Session, workflow: Workflow) -> dict:
    """A·B를 통째로 다시 계산한다(C 제안은 남긴다). 저장소 색인이 없으면 그 저장소만 건너뛴다."""
    stores = stores_of(db, workflow)
    db.execute(delete(WorkflowLink).where(WorkflowLink.workflow_id == workflow.id,
                                          WorkflowLink.source.in_(_RECOMPUTED)))
    links = _from_design(workflow) + _from_runs(db, workflow, stores)
    skipped = []
    for store in stores:
        try:
            links += _from_names(workflow, [store])
        except Exception as e:  # noqa: BLE001 — 색인이 아직 없거나 깨진 저장소 하나가 전체를 막지 않게
            skipped.append(f"{store.name}: {str(e)[:120]}")
    db.add_all(links)
    db.commit()
    counts: dict[str, int] = {}
    for link in links:
        counts[link.source] = counts.get(link.source, 0) + 1
    return {"stores": [s.name for s in stores], "counts": counts, "skipped": skipped}


def refresh_for_store(db: Session, store_name: str) -> int:
    """저장소가 재색인된 뒤 — 그 저장소를 쓰는 워크플로의 연결을 다시 계산한다."""
    target = storage.store(store_name, db)
    if target is None or target.organization_id is None:
        return 0
    n = 0
    for row in db.execute(select(Workflow).where(Workflow.organization_id == target.organization_id)).scalars():
        if any(s.name == store_name for s in stores_of(db, row)):
            compute(db, row)
            n += 1
    return n


def links_of(db: Session, workflow: Workflow) -> list[WorkflowLink]:
    return list(db.execute(select(WorkflowLink).where(WorkflowLink.workflow_id == workflow.id)
                           .order_by(WorkflowLink.subject_kind, WorkflowLink.subject,
                                     WorkflowLink.rel, WorkflowLink.id)).scalars())


def unground_rules(db: Session, workflow: Workflow) -> list[str]:
    """근거 조문·문서가 이어지지 않은 규칙 — 워크플로 점검이 "근거 미확인"으로 보인다."""
    grounded = {link.subject for link in links_of(db, workflow)
                if link.rel == "grounded_in" and link.confirmed}
    return [name for kind, name in subjects(workflow) if kind == "constraint" and name not in grounded]


def prompt_lines(db: Session, workflow: Workflow, limit: int = 15) -> tuple[list[str], list[str]]:
    """스마트워크 업무 맥락에 싣는 것 — (이 워크플로의 저장소, 근거 줄). 확인된 연결만."""
    stores = [s.name for s in stores_of(db, workflow)]
    lines = []
    for link in links_of(db, workflow):
        if not link.confirmed or link.rel not in ("matches", "grounded_in", "mentions"):
            continue
        where = f"{link.store}/{link.path}" if link.path else link.store
        label = {"matches": "같은 이름", "grounded_in": "근거", "mentions": "많이 나오는 문서"}[link.rel]
        lines.append(f"{link.subject[:60]} → {label}: {link.target_kind} {link.target[:80]} ({where})")
        if len(lines) >= limit:
            break
    return stores, lines


def graph(db: Session, workflow: Workflow) -> dict:
    """워크플로를 가운데 둔 그림 — 워크플로 → 대상·상태·규칙·단계 → 저장소의 노드."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def node(nid: str, label: str, kind: str, **extra) -> str:
        nodes.setdefault(nid, {"id": nid, "label": label[:80], "kind": kind, **extra})
        return nid

    center = node(f"wf:{workflow.id}", workflow.name, "workflow")
    attached: set[str] = set()

    def attach(sid: str) -> None:
        if sid not in attached:
            attached.add(sid)
            edges.append({"source": center, "target": sid, "label": ""})

    for link in links_of(db, workflow):
        sid = node(f"{link.subject_kind}:{link.subject}", link.subject, link.subject_kind)
        attach(sid)
        tid = node(f"{link.store}|{link.target_kind}:{link.target}", link.target.rsplit("/", 1)[-1],
                   link.target_kind, store=link.store, path=link.path)
        edges.append({"source": sid, "target": tid, "label": link.rel, "link_id": link.id,
                      "confirmed": link.confirmed, "source_kind": link.source})
    for kind, name in subjects(workflow):
        attach(node(f"{kind}:{name}", name, kind))
    return {"nodes": list(nodes.values()), "links": edges}


# --------------------------------------------------------------- C. LLM 제안

SUGGEST_PROMPT = """사내 업무 워크플로의 낱말을 부서 문서의 온톨로지 노드와 잇는다. 아래 '후보' 중에서만 고른다 —
후보에 없는 것을 지어내지 마라. 맞는 것이 없으면 고르지 않는다. 출력은 JSON 객체 하나다(설명·코드펜스 없이).

{{"links": [{{"subject": 낱말 번호, "candidate": 후보 번호, "reason": "왜 같은 것인지 한 문장"}}]}}

규칙:
- 대상·상태는 뜻이 같은 용어·조문·표를 고른다(다른 표현·동의어·줄임말 포함). 이름만 비슷하고 뜻이 다르면 고르지 않는다.
- 규칙은 그 규칙의 근거가 되는 조문·문서를 고른다.
- 낱말 하나에 후보 셋까지.

워크플로: {name}
낱말:
{subjects}

후보:
{candidates}
"""
MAX_CANDIDATES = 120
_KIND_LABEL = {"entity": "대상", "state": "상태", "constraint": "규칙"}


def _tokens(text: str) -> list[str]:
    words = [w for w in re.split(r"[\s,.·/()「」〈〉\"'“”‘’:;]+", text) if len(w) >= MIN_NAME]
    # 조사 하나만 떼어 본다 — 정확한 형태소 분석이 아니라 후보를 넓히는 정도면 된다.
    return list(dict.fromkeys(re.sub(r"(은|는|이|가|을|를|의|에|에서|으로|로|와|과|도)$", "", w) for w in words))


def suggest(db: Session, workflow: Workflow, chat) -> dict:
    """B가 잇지 못한 낱말에 LLM이 짝을 제안한다 — 후보 밖은 버리고, 확인 전(confirmed=False)으로 둔다.

    chat(prompt) -> 응답 문자열. LLM 호출은 바깥이 정한다(프로바이더·관측 기록).
    """
    existing = links_of(db, workflow)
    linked = {(link.subject_kind, link.subject) for link in existing
              if link.rel in ("matches", "grounded_in")}
    todo = [(k, n) for k, n in subjects(workflow) if (k, n) not in linked]
    if not todo:
        return {"suggested": 0, "note": "모든 낱말이 이미 이어져 있습니다."}
    stores = stores_of(db, workflow)
    candidates: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for _, name in todo:
        for token in _tokens(name)[:6]:
            for store in stores:
                try:
                    rows = docsearch.node_search(store.name, q=token, limit=8)
                except Exception:  # noqa: BLE001
                    continue
                for row in rows:
                    key = (store.name, row["kind"], row["name"])
                    if row["kind"] in ("term", "section", "table", "document") and key not in seen:
                        seen.add(key)
                        candidates.append({"store": store.name, "kind": row["kind"], "name": row["name"],
                                           "detail": (row.get("detail") or "")[:80]})
            if len(candidates) >= MAX_CANDIDATES:
                break
    if not candidates:
        return {"suggested": 0, "note": "온톨로지에서 후보를 찾지 못했습니다 — 저장소 색인을 확인하세요."}
    text = SUGGEST_PROMPT.format(
        name=workflow.name,
        subjects="\n".join(f"{i}. [{_KIND_LABEL[k]}] {n}" for i, (k, n) in enumerate(todo)),
        candidates="\n".join(f"{i}. {c['kind']} · {c['name']}" + (f" ({c['detail']})" if c["detail"] else "")
                             + f" — 저장소 {c['store']}" for i, c in enumerate(candidates)))
    raw = chat(text)
    data = _parse(raw)
    have = {(link.subject, link.store, link.target_kind, link.target) for link in existing}
    made = 0
    for item in data.get("links") if isinstance(data.get("links"), list) else []:
        if not isinstance(item, dict):
            continue
        si, ci = item.get("subject"), item.get("candidate")
        if not (isinstance(si, int) and 0 <= si < len(todo) and isinstance(ci, int) and 0 <= ci < len(candidates)):
            continue          # 목록 밖 번호는 버린다
        kind, name = todo[si]
        c = candidates[ci]
        if (name, c["store"], c["kind"], c["name"]) in have:
            continue
        have.add((name, c["store"], c["kind"], c["name"]))
        rel = "grounded_in" if kind == "constraint" else "matches"
        db.add(_link(workflow, kind, name, rel, c["store"], c["kind"], c["name"], "llm",
                     detail=str(item.get("reason") or "")[:300], confirmed=False))
        made += 1
    db.commit()
    return {"suggested": made, "note": "" if made else "LLM이 맞는 짝을 고르지 않았습니다."}


def _parse(text: str) -> dict:
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", (text or "").strip())
    for candidate in (cleaned, cleaned[cleaned.find("{"):cleaned.rfind("}") + 1]):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}
