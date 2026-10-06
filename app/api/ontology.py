"""온톨로지 관리 창구 — 문서가 그래프로 얼마나 옮겨졌는지, 어디서 멈췄는지.

조회는 색인 DB만 읽는다(services/ontology_status). 전환을 **진행시키는** 유일한 수단은
재색인이고, 그건 이미 있는 경로를 그대로 쓴다(docsearch.reindex) — 온톨로지는 색인의
파생물이라 별도의 "전환" 작업이 따로 있지 않다.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey
from ..security import require_admin, require_api_key
from ..services import docsearch, ontology_status
from ..services import storage as storage_service

router = APIRouter(tags=["ontology"])

# 재색인 한 번에 쓸 시간 예산(초). MCP 경로와 같은 이유로 나눠 돈다 — 한 번에 끝내지 않고
# 남은 개수를 돌려주면 화면이 진행을 보여 줄 수 있다(docsearch.reindex).
_REINDEX_BUDGET = 20.0


@router.get("/ontology/overview")
def ontology_overview(_: ApiKey = Depends(require_api_key)):
    """전환 현황 전체 — 저장소별 수치, 합계, 퍼널, 되풀이 표 스키마, 실패 이유.

    **아무것도 바꾸지 않는다.** 숫자마다 근거를 댈 수 있게 노드가 0건인 문서 경로도 함께
    준다 — "전환율 86%"보다 "이 문서들이 안 됐다"가 고치는 데 쓸모 있다.
    """
    try:
        return ontology_status.overview()
    except storage_service.StorageError as e:
        # 저장소 설정 오류다 — 요청이 잘못된 것이 아니다.
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ontology/stores/{store_name}")
def ontology_store(store_name: str, _: ApiKey = Depends(require_api_key)):
    """저장소 하나의 전환 현황(목록 화면에서 펼쳐 볼 때)."""
    if storage_service.store(store_name) is None:
        raise HTTPException(status_code=404, detail=f"storage '{store_name}' not found")
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
    _: ApiKey = Depends(require_api_key),
):
    """노드 찾기 — 화면이 종류별 예시를 보여 주고 사람이 그래프로 내려갈 때 쓴다.

    종류 수준(문서·절·용어·표)과 관계 종류는 overview가 이미 준다. 여기서는 그 아래,
    실제 노드 몇 개를 본다 — 수치가 맞는지는 결국 이름을 봐야 믿을 수 있다.
    """
    if storage_service.store(store_name) is None:
        raise HTTPException(status_code=404, detail=f"storage '{store_name}' not found")
    nodes = docsearch.find_nodes(store_name, kind, q, max(1, min(limit, 200)))
    return {"store": store_name, "kind": kind, "q": q, "nodes": nodes}


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
    if store is None:
        raise HTTPException(status_code=404, detail=f"storage '{store_name}' not found")
    result = docsearch.reindex(store.name, store.root, force=force,
                              budget_seconds=_REINDEX_BUDGET)
    audit.record(db, admin.name, "ontology.reindex", store.name, result)
    return result
