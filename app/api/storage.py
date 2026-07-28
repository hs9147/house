"""파일 저장소 창구 — file_storage 모듈의 로컬 경로를 URL 뒤에 감춘다."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, Module, ModuleType
from ..security import require_api_key
from ..services import storage as storage_service

router = APIRouter(tags=["storage"])


def _module(db: Session, module_name: str) -> Module:
    row = db.execute(select(Module).where(Module.name == module_name)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail=f"storage module '{module_name}' not found")
    if row.type != ModuleType.file_storage:
        raise HTTPException(status_code=400, detail=f"module '{module_name}' is not a file_storage module")
    return row


@router.get("/storage/{module_name}/files")
def list_storage_files(
    module_name: str,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    module = _module(db, module_name)
    return {
        "module": module.name,
        "url": storage_service.url_for(module.name),
        "files": storage_service.list_files(storage_service.root_for(module)),
    }


@router.get("/storage/{module_name}/files/content")
def download_storage_file(
    module_name: str,
    path: str,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    module = _module(db, module_name)
    try:
        target = storage_service.resolve(storage_service.root_for(module), path)
    except storage_service.StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, filename=target.name)


@router.post("/storage/{module_name}/files", status_code=201)
def upload_storage_file(
    module_name: str,
    file: UploadFile = File(...),
    path: str = Form(""),
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """path를 주면 그 이름으로, 비우면 업로드한 파일명 그대로 저장한다."""
    module = _module(db, module_name)
    rel = (path or file.filename or "").strip()
    if not rel:
        raise HTTPException(status_code=400, detail="file name required")
    try:
        saved = storage_service.write_file(storage_service.root_for(module), rel, file.file.read())
    except storage_service.StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.record(db, key.name, "storage.upload", module.name, {"path": saved})
    return {"path": saved}


@router.delete("/storage/{module_name}/files", status_code=204)
def delete_storage_file(
    module_name: str,
    path: str,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    module = _module(db, module_name)
    try:
        storage_service.delete_file(storage_service.root_for(module), path)
    except storage_service.StorageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="file not found")
    audit.record(db, key.name, "storage.delete", module.name, {"path": path})
    return None
