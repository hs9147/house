"""온톨로지 관리 창구 — 문서가 그래프로 얼마나 옮겨졌는지, 어디서 멈췄는지.

조회는 색인 DB만 읽는다(services/ontology_status). 전환을 **진행시키는** 유일한 수단은
재색인이고, 그건 이미 있는 경로를 그대로 쓴다(docsearch.reindex) — 온톨로지는 색인의
파생물이라 별도의 "전환" 작업이 따로 있지 않다.
"""
from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey
from ..security import (accessible_document_stores, require_admin, require_api_key,
                        require_document_store)
from ..services import docsearch, indexclean, ontology_status, semantic
from ..services import storage as storage_service

router = APIRouter(tags=["ontology"])


def _indexed_store(db: Session, key: ApiKey, store_name: str):
    """권한과 함께 **색인 대상인지**(storage.indexed) — 내부 저장소(internal)는 색인하지 않아 그래프가 없다.
    없는 저장소와 같은 404로 답한다."""
    found = require_document_store(db, key, store_name)
    if not storage_service.indexed(found):
        raise HTTPException(status_code=404, detail=f"'{store_name}'은 색인 대상이 아닙니다(내부 저장소)")
    return found

# 재색인 한 번에 쓸 시간 예산(초). MCP 경로와 같은 이유로 나눠 돈다 — 한 번에 끝내지 않고
# 남은 개수를 돌려주면 화면이 진행을 보여 줄 수 있다(docsearch.reindex).
_REINDEX_BUDGET = 20.0


@router.get("/ontology/overview")
def ontology_overview(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    """전환 현황 전체 — 저장소별 수치, 합계, 퍼널, 되풀이 표 스키마, 실패 이유.

    **아무것도 바꾸지 않는다.** 숫자마다 근거를 댈 수 있게 노드가 0건인 문서 경로도 함께
    준다 — "전환율 86%"보다 "이 문서들이 안 됐다"가 고치는 데 쓸모 있다.
    """
    try:
        return ontology_status.overview(accessible_document_stores(db, key))
    except storage_service.StorageError as e:
        # 저장소 설정 오류다 — 요청이 잘못된 것이 아니다.
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ontology/stores/{store_name}")
def ontology_store(store_name: str, db: Session = Depends(get_db),
                   key: ApiKey = Depends(require_api_key)):
    """저장소 하나의 전환 현황(목록 화면에서 펼쳐 볼 때)."""
    _indexed_store(db, key, store_name)
    return {
        **ontology_status.store_summary(store_name),
        "index_bytes": ontology_status.index_file_size(store_name),
    }


@router.get("/ontology/stores/{store_name}/graph")
def ontology_store_graph(
    store_name: str,
    kind: str = "",
    q: str = "",
    limit: int = 40,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """노드 찾기 — 화면이 종류별 예시를 보여 주고 사람이 그래프로 내려갈 때 쓴다.

    종류 수준(문서·절·용어·표)과 관계 종류는 overview가 이미 준다. 여기서는 그 아래,
    실제 노드 몇 개를 본다 — 수치가 맞는지는 결국 이름을 봐야 믿을 수 있다.
    """
    _indexed_store(db, key, store_name)
    nodes = docsearch.node_search(store_name, kind, q, max(1, min(limit, 200)))
    return {"store": store_name, "kind": kind, "q": q, "nodes": nodes}


@router.get("/ontology/stores/{store_name}/graph/neighbors")
def ontology_store_neighbors(
    store_name: str,
    kind: str,
    name: str,
    limit: int = 40,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """이 노드에 붙은 것들 — 정보 조회 화면이 한 걸음 펼칠 때 부른다.

    전체 그래프를 한 번에 주지 않는다. 수만 노드를 그리면 아무 질문에도 답하지 못하는
    털뭉치가 되고, 그래서 탐색의 규약은 **찾고(검색) · 문맥을 보고(이웃) · 필요한 데서만
    펼친다**(van Ham & Perer 2009). 서버가 주는 단위도 그 한 걸음이다.
    """
    _indexed_store(db, key, store_name)
    if not name:
        raise HTTPException(status_code=422, detail="name이 필요합니다")
    return docsearch.neighborhood(store_name, kind, name, max(1, min(limit, 200)))


@router.post("/ontology/stores/{store_name}/reindex")
def ontology_reindex(
    store_name: str,
    force: bool = False,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """전환을 진행시킨다 — 색인을 돌리면 그래프가 같은 추출에서 다시 만들어진다.

    별도의 "온톨로지 변환" 작업을 만들지 않는 이유: 그래프는 색인의 파생물이고, 두 경로를
    두면 "색인은 됐는데 그래프는 옛 것"인 상태가 생긴다. 시간 예산 안에서 돌고 남은 개수를
    돌려주므로, 화면이 이어서 다시 부르면 된다.
    """
    store = storage_service.store(store_name)
    if store is None or not storage_service.indexed(store):
        raise HTTPException(status_code=404, detail=f"storage '{store_name}' not found")
    result = docsearch.reindex(store.name, store.root, force=force,
                              budget_seconds=_REINDEX_BUDGET)
    audit.record(db, admin.name, "ontology.reindex", store.name, result)
    return result


@router.get("/ontology/cleanse")
def ontology_cleanse_preview(admin: ApiKey = Depends(require_admin)):
    """클린징 미리보기 — 지울 찌꺼기(색인 대상이 아닌 저장소의 색인·.ready, 옛 노드, 빠진 문서의 내용 그래프·.ready)를
    세기만 한다. 실제로 지우는 것(POST)과 같은 함수다."""
    return indexclean.cleanse(apply=False)


@router.post("/ontology/cleanse")
def ontology_cleanse(db: Session = Depends(get_db), admin: ApiKey = Depends(require_admin)):
    """클린징 — 파생 데이터의 찌꺼기만 지운다(원본 문서는 건드리지 않는다). 지운 것은 감사에 남긴다."""
    result = indexclean.cleanse(apply=True)
    audit.record(db, admin.name, "ontology.cleanse", "*", {
        "orphan_index": [i["store"] for i in result["orphan_index"]],
        "orphan_ready": [i["store"] for i in result["orphan_ready"]],
        "stores": result["stores"]})
    return result


# --- 내용 온톨로지(services/semantic) — 형식 그래프와 따로, LLM이 내용에서 뽑는다 ---

def _semantic(fn, *args):
    try:
        return fn(*args)
    except semantic.SemanticError as e:
        raise HTTPException(status_code=409, detail=str(e))


def _store_or_404(store_name: str):
    store = storage_service.store(store_name)
    if store is None or not storage_service.indexed(store):
        raise HTTPException(status_code=404, detail=f"storage '{store_name}' not found")
    return store


def _submit(store_name: str, label: str, fn) -> None:
    if not semantic.submit(store_name, label, fn):
        raise HTTPException(status_code=409, detail="이 저장소에서 이미 돌고 있는 제안이 있습니다 — 끝난 뒤에 다시 하세요.")


@router.get("/ontology/stores/{store_name}/semantic")
def semantic_status(store_name: str, db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    """스키마(초안·확정)·분석 진행·표본 검토·스키마 밖 후보·묶기 — 화면이 이것 하나를 다시 읽는다."""
    _indexed_store(db, key, store_name)
    return semantic.status(store_name)


@router.post("/ontology/stores/{store_name}/semantic/schema/propose", status_code=202)
def semantic_propose_schema(store_name: str, db: Session = Depends(get_db),
                            admin: ApiKey = Depends(require_admin)):
    """① 스키마 제안 → LLM 검토 → 한 번 고침. 뒤에서 돈다(LLM 여러 번) — 상태의 task·schema_draft로 본다."""
    _store_or_404(store_name)
    _semantic(semantic.provider_for, db, store_name)   # 프로바이더가 없으면 지금 말한다(뒤에서 실패하지 않게)
    _submit(store_name, "스키마 제안·검토", lambda s, report: semantic.propose_schema(s, store_name, report))
    audit.record(db, admin.name, "ontology.semantic.schema.propose", store_name, {})
    return {"started": True}


@router.put("/ontology/stores/{store_name}/semantic/schema")
def semantic_confirm_schema(store_name: str, schema: dict = Body(...), db: Session = Depends(get_db),
                            admin: ApiKey = Depends(require_admin)):
    """관리자가 확정 — 판이 바뀌면 문서 분석은 새 판으로 다시 돈다."""
    _store_or_404(store_name)
    out = _semantic(semantic.confirm_schema, store_name, schema, admin.name)
    audit.record(db, admin.name, "ontology.semantic.schema.confirm", store_name,
                 {"version": out["version"], "classes": len(out["classes"]), "relations": len(out["relations"])})
    return out


@router.get("/ontology/stores/{store_name}/semantic/forms")
def semantic_forms(store_name: str, db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    """② 되풀이 양식과 각각의 열 대응표(제안·검토·확정)."""
    _indexed_store(db, key, store_name)
    return {"forms": semantic.forms(store_name)}


@router.post("/ontology/stores/{store_name}/semantic/forms/propose", status_code=202)
def semantic_propose_mapping(store_name: str, form: str = Body(..., embed=True), db: Session = Depends(get_db),
                             admin: ApiKey = Depends(require_admin)):
    _store_or_404(store_name)
    _semantic(semantic.provider_for, db, store_name)
    _submit(store_name, f"양식 대응 제안·검토: {form[:60]}", lambda s, report: semantic.propose_mapping(s, store_name, form, report))
    audit.record(db, admin.name, "ontology.semantic.form.propose", store_name, {"form": form[:200]})
    return {"started": True}


@router.put("/ontology/stores/{store_name}/semantic/forms")
def semantic_confirm_mapping(store_name: str, form: str = Body(...), mapping: dict = Body(...),
                             db: Session = Depends(get_db), admin: ApiKey = Depends(require_admin)):
    _store_or_404(store_name)
    out = _semantic(semantic.confirm_mapping, store_name, form, mapping, admin.name)
    audit.record(db, admin.name, "ontology.semantic.form.confirm", store_name, {"form": form[:200]})
    return out


@router.post("/ontology/stores/{store_name}/semantic/analyze")
def semantic_analyze(store_name: str, action: str = "start", db: Session = Depends(get_db),
                     admin: ApiKey = Depends(require_admin)):
    """③ 문서 분석 — start(뒤에서 끝까지)·stop·resume(멈춤 조건으로 멈춘 것을 다시, 표본 집계를 비운다)."""
    _store_or_404(store_name)
    if action == "stop":
        semantic.stop(store_name)
    elif action in ("start", "resume"):
        _semantic(semantic.provider_for, db, store_name)
        if not semantic.status(store_name)["schema"]:
            raise HTTPException(status_code=409, detail="확정한 스키마가 없습니다 — ① 스키마를 먼저 확정하세요.")
        if action == "resume":
            semantic.resume(store_name)
        semantic.start(store_name)
    else:
        raise HTTPException(status_code=422, detail="action은 start·stop·resume 중 하나입니다.")
    audit.record(db, admin.name, f"ontology.semantic.analyze.{action}", store_name, {})
    return {"running": semantic.running(store_name)}


@router.post("/ontology/stores/{store_name}/semantic/merge", status_code=202)
def semantic_merge(store_name: str, db: Session = Depends(get_db), admin: ApiKey = Depends(require_admin)):
    """④ 같은 개체 묶기 — 정규화가 같은 것은 바로, 애매한 것은 LLM 제안 → 묶음마다 LLM 검토."""
    _store_or_404(store_name)
    _semantic(semantic.provider_for, db, store_name)
    _submit(store_name, "같은 개체 묶기", lambda s, report: semantic.propose_merges(s, store_name, report))
    audit.record(db, admin.name, "ontology.semantic.merge", store_name, {})
    return {"started": True}


@router.delete("/ontology/stores/{store_name}/semantic/aliases", status_code=204)
def semantic_unmerge(store_name: str, cls: str, alias: str, db: Session = Depends(get_db),
                     admin: ApiKey = Depends(require_admin)):
    """묶음 풀기 — 별칭 하나를 지운다(그 이름은 다시 따로 보인다)."""
    _store_or_404(store_name)
    semantic.remove_alias(store_name, cls, alias)
    audit.record(db, admin.name, "ontology.semantic.unmerge", store_name, {"cls": cls, "alias": alias})
    return None


@router.get("/ontology/stores/{store_name}/semantic/search")
def semantic_search(store_name: str, cls: str = "", q: str = "", limit: int = 40,
                    db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    _indexed_store(db, key, store_name)
    return {"nodes": semantic.search(store_name, cls, q, max(1, min(limit, 200)))}


@router.get("/ontology/stores/{store_name}/semantic/neighbors")
def semantic_neighbors(store_name: str, cls: str, name: str, limit: int = 60,
                       db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    _indexed_store(db, key, store_name)
    return semantic.neighbors(store_name, cls, name, max(1, min(limit, 200)))
