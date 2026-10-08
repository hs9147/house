"""부서별 DB 저장소 등록과 파일 창구. 내부 저장소는 관리자만 직접 접근한다."""
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, DocumentStore, Organization
from ..security import (accessible_document_stores, require_admin, require_api_key,
                        require_document_store)
from ..services import storage as storage_service
from ..services.storage import Store

router = APIRouter(tags=["storage"])


def _store(db: Session, key: ApiKey, store_name: str) -> Store:
    return require_document_store(db, key, store_name)


def _require_writable(store: Store) -> None:
    """읽기 전용 저장소의 쓰기와 삭제를 막는다."""
    if store.read_only:
        raise HTTPException(status_code=403, detail=f"storage '{store.name}' is read-only")


@router.get("/storage/stores")
def list_stores(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    """소속 부서의 저장소 목록. 절대 경로는 관리자에게만 보인다."""
    try:
        visible = accessible_document_stores(db, key)
    except storage_service.StorageError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return [
        {"name": s.name, "root": str(s.root) if key.is_admin else "",
         "organization_id": s.organization_id, "read_only": s.read_only,
         "exists": s.root.is_dir(), "url": storage_service.url_for(s.name)}
        for s in visible
    ]


class StoreCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")
    root: str
    organization_id: int
    read_only: bool = False


class StoreUpdate(BaseModel):
    organization_id: int
    read_only: bool
    active: bool = True


@router.post("/storage/stores", status_code=201)
def create_store(body: StoreCreate, db: Session = Depends(get_db),
                 admin: ApiKey = Depends(require_admin)):
    """이름/경로는 생성 후 불변: 온톨로지와 .ready 캐시의 식별자가 된다."""
    if db.get(Organization, body.organization_id) is None:
        raise HTTPException(status_code=404, detail="조직을 찾을 수 없습니다")
    if not Path(body.root).is_absolute():
        raise HTTPException(status_code=400, detail="절대 디렉터리 경로가 필요합니다")
    try:
        root = storage_service.check_allowed_root(Path(body.root))
    except storage_service.StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="존재하는 절대 디렉터리 경로가 필요합니다")
    if body.name == storage_service.INTERNAL_STORE or db.execute(
        select(DocumentStore).where(DocumentStore.name == body.name)
    ).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="저장소 이름이 이미 사용 중입니다")
    registered = [Path(r.root_path).resolve() for r in db.execute(select(DocumentStore)).scalars()]
    registered.append(Path(storage_service.store(storage_service.INTERNAL_STORE, db).root))
    if any(root == path or root.is_relative_to(path) or path.is_relative_to(root)
           for path in registered):
        raise HTTPException(status_code=409, detail="기존 저장소와 경로가 겹칩니다")
    row = DocumentStore(name=body.name, root_path=str(root),
                        organization_id=body.organization_id, read_only=body.read_only,
                        active=True)
    db.add(row)
    db.commit()
    audit.record(db, admin.name, "storage.create", row.name,
                 {"organization_id": body.organization_id, "root": str(root)})
    return {"name": row.name, "organization_id": row.organization_id,
            "root": row.root_path, "read_only": row.read_only, "active": row.active}


@router.patch("/storage/stores/{store_name}")
def update_store(store_name: str, body: StoreUpdate, db: Session = Depends(get_db),
                 admin: ApiKey = Depends(require_admin)):
    row = db.execute(select(DocumentStore).where(DocumentStore.name == store_name)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="저장소를 찾을 수 없습니다")
    if db.get(Organization, body.organization_id) is None:
        raise HTTPException(status_code=404, detail="조직을 찾을 수 없습니다")
    row.organization_id = body.organization_id
    row.read_only = body.read_only
    row.active = body.active
    db.commit()
    audit.record(db, admin.name, "storage.update", row.name,
                 {"organization_id": row.organization_id, "read_only": row.read_only,
                  "active": row.active})
    return {"name": row.name, "organization_id": row.organization_id,
            "root": row.root_path, "read_only": row.read_only, "active": row.active}


@router.get("/storage/{store_name}/files")
def list_storage_files(
    store_name: str,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    store = _store(db, key, store_name)
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
    store = _store(db, key, store_name)
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
    store = _store(db, key, store_name)
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
    store = _store(db, key, store_name)
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
