"""파일 저장소 창구 — 환경변수로 정한 저장소를 이름으로만 다루게 한다.

저장소 목록은 PAAS_STORAGE_ROOT·PAAS_DOC_ROOTS가 정한다(services/storage.py).
사내 문서 폴더는 읽기 전용이라 이 창구로도 쓰기·삭제가 되지 않는다.
"""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey
from ..security import require_api_key
from ..services import storage as storage_service
from ..services.storage import Store

router = APIRouter(tags=["storage"])


def _stores() -> list[Store]:
    try:
        return storage_service.stores()
    except storage_service.StorageError as e:
        # 설정 오류다 — 요청이 잘못된 게 아니라 서버 환경변수가 잘못돼 있다.
        raise HTTPException(status_code=500, detail=str(e))


def _store(store_name: str) -> Store:
    found = next((s for s in _stores() if s.name == store_name), None)
    if found is None:
        raise HTTPException(status_code=404, detail=f"storage '{store_name}' not found")
    return found


def _require_writable(store: Store) -> None:
    """사내 문서 폴더는 읽으러 붙인 것이라 콘솔에서 실수로 지워지는 일까지 막는다."""
    if store.read_only:
        raise HTTPException(status_code=403, detail=f"storage '{store.name}' is read-only")


@router.get("/storage/stores")
def list_stores(_: ApiKey = Depends(require_api_key)):
    """고를 수 있는 저장소 목록 — 사내 문서 폴더들. root를 함께 준다: 환경변수로 정하는
    값이라 "무엇이 붙었는지"를 되비춰 주지 않으면 운영자가 확인할 방법이 없다.

    플랫폼 자신의 저장소(internal)는 빠진다. 아래 파일 엔드포인트는 이름을 주면 그대로
    받으므로, 예전에 거기 올린 파일은 계속 꺼낼 수 있다.
    """
    try:
        visible = storage_service.visible_stores()
    except storage_service.StorageError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return [
        {"name": s.name, "root": str(s.root), "read_only": s.read_only,
         "exists": s.root.is_dir(), "url": storage_service.url_for(s.name)}
        for s in visible
    ]


@router.get("/storage/{store_name}/files")
def list_storage_files(
    store_name: str,
    _: ApiKey = Depends(require_api_key),
):
    store = _store(store_name)
    return {
        "store": store.name,
        "read_only": store.read_only,
        "url": storage_service.url_for(store.name),
        "files": storage_service.list_files(store.root),
    }


@router.get("/storage/{store_name}/files/content")
def download_storage_file(
    store_name: str,
    path: str,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    store = _store(store_name)
    try:
        target = storage_service.resolve(store.root, path)
    except storage_service.StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    # 사내망 전제라 별도 자격증명을 요구하지 않는 대신, 파일을 꺼내 간 주체는 남긴다.
    # key.name은 발급 키 이름이거나 OIDC preferred_username이다.
    audit.record(db, key.name, "storage.download", store.name, {"path": path})
    return FileResponse(target, filename=target.name)


@router.post("/storage/{store_name}/files", status_code=201)
def upload_storage_file(
    store_name: str,
    file: UploadFile = File(...),
    path: str = Form(""),
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """path를 주면 그 이름으로, 비우면 업로드한 파일명 그대로 저장한다."""
    store = _store(store_name)
    _require_writable(store)
    rel = (path or file.filename or "").strip()
    if not rel:
        raise HTTPException(status_code=400, detail="file name required")
    try:
        saved = storage_service.write_file(store, rel, file.file.read())
    except storage_service.StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(db, key.name, "storage.upload", store.name, {"path": saved})
    return {"path": saved}


@router.delete("/storage/{store_name}/files", status_code=204)
def delete_storage_file(
    store_name: str,
    path: str,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    store = _store(store_name)
    _require_writable(store)
    try:
        grave = storage_service.delete_file(store, path)
    except storage_service.StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file not found")
    # 어디로 갔는지 남긴다 — 되돌릴 수 있다는 사실은 자리를 알아야 쓸모가 있다.
    audit.record(db, key.name, "storage.delete", store.name,
                 {"path": path, "trashed_to": grave})
    return None
