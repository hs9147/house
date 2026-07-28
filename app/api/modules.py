import io
from pathlib import Path
import zipfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..models import ApiKey, Module, ModuleBinding, ModuleType, Organization, Project
from ..schemas import ApiModuleImport, ModuleBind, ModuleCreate
from ..security import require_admin, require_api_key
from ..services import apisearch
from ..services import mcp_search
from ..services import modules as svc

router = APIRouter(tags=["modules"])


@router.post("/modules", status_code=201)
def create_module(
    body: ModuleCreate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    if db.execute(select(Module).where(Module.name == body.name)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="module name already exists")
    if body.organization_id is not None and db.get(Organization, body.organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    
    # file_storage 일 경우 root 경로 하위에 지정된 폴더가 없으면 자동 생성
    if body.type == ModuleType.file_storage.value and body.config:
        sub_folder = body.config.get("sub_folder") or body.config.get("bucket")
        if sub_folder:
            settings = get_settings()
            root_path = Path(body.config.get("endpoint") or settings.storage_root or "./data/storage").resolve()
            target_dir = root_path / str(sub_folder).strip("/\\")
            target_dir.mkdir(parents=True, exist_ok=True)

    row = Module(
        name=body.name, type=ModuleType(body.type), category=body.category,
        organization_id=body.organization_id, config=svc.encrypt_config(body.config),
    )
    db.add(row)
    db.commit()
    audit.record(db, key.name, "module.create", body.name, {"type": body.type})
    return {"id": row.id, "name": row.name, "type": row.type.value, "category": row.category,
            "organization_id": row.organization_id, "config": svc.masked_config(row.config)}


@router.post("/modules/upload-storage", status_code=201)
def upload_file_storage_module(
    zip_file: UploadFile = File(...),
    name: str = Form(...),
    category: str = Form(None),
    organization_id: int = Form(None),
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """ZIP 파일 업로드 시 root 경로에 파일명으로 폴더를 생성하고 압축 해제 후 모듈을 자동 등록한다."""
    if db.execute(select(Module).where(Module.name == name)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="module name already exists")
    if organization_id is not None and db.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    
    filename = zip_file.filename or "storage.zip"
    folder_name = Path(filename).stem.strip() or "uploaded-storage"
    
    settings = get_settings()
    root_path = Path(settings.storage_root or "./data/storage").resolve()
    target_dir = root_path / folder_name
    target_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        content = zip_file.file.read()
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            zf.extractall(target_dir)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"ZIP 파일 압축 해제 실패: {e}")

    config = {"endpoint": str(root_path), "sub_folder": folder_name, "bucket": folder_name}
    row = Module(
        name=name, type=ModuleType.file_storage, category=category,
        organization_id=organization_id, config=svc.encrypt_config(config),
    )
    db.add(row)
    db.commit()
    audit.record(db, key.name, "module.create_zip_storage", name, {"folder_name": folder_name})
    return {"id": row.id, "name": row.name, "type": row.type.value, "category": row.category,
            "organization_id": row.organization_id, "config": svc.masked_config(row.config)}


@router.get("/modules")
def list_modules(db: Session = Depends(get_db), _: ApiKey = Depends(require_api_key)):
    rows = db.execute(select(Module).order_by(Module.id)).scalars()
    return [
        {"id": m.id, "name": m.name, "type": m.type.value, "category": m.category,
         "organization_id": m.organization_id, "config": svc.masked_config(m.config)}
        for m in rows
    ]


@router.put("/modules/{module_id}")
def update_module(
    module_id: int,
    body: ModuleCreate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    row = db.get(Module, module_id)
    if row is None:
        raise HTTPException(status_code=404, detail="module not found")
    row.name = body.name
    row.category = body.category
    row.organization_id = body.organization_id
    if body.config:
        existing_cfg = svc.decrypt_config(row.config or {})
        existing_cfg.update(body.config)
        row.config = svc.encrypt_config(existing_cfg)
    db.commit()
    audit.record(db, key.name, "module.update", row.name, {"type": row.type.value})
    return {"id": row.id, "name": row.name, "type": row.type.value, "category": row.category,
            "organization_id": row.organization_id, "config": svc.masked_config(row.config)}


@router.delete("/modules/{module_id}", status_code=204)
def delete_module(
    module_id: int,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """admin 권한으로 모듈을 삭제한다."""
    row = db.get(Module, module_id)
    if row is None:
        raise HTTPException(status_code=404, detail="module not found")

    bindings = db.execute(select(ModuleBinding).where(ModuleBinding.module_id == module_id)).scalars().all()
    for b in bindings:
        db.delete(b)

    mod_name = row.name
    db.delete(row)
    db.commit()
    audit.record(db, admin.name, "module.delete", mod_name, {"module_id": module_id})
    return None


@router.post("/projects/{project_id}/modules/{module_id}/bind", status_code=201)
def bind_module(
    project_id: int,
    module_id: int,
    body: ModuleBind,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, project_id)
    module = db.get(Module, module_id)
    if project is None or module is None:
        raise HTTPException(status_code=404, detail="project or module not found")
    dup = db.execute(
        select(ModuleBinding).where(
            ModuleBinding.project_id == project_id,
            ModuleBinding.env_prefix == body.env_prefix,
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(status_code=409, detail="env_prefix already used in this project")
    db.add(ModuleBinding(project_id=project_id, module_id=module_id, env_prefix=body.env_prefix))
    db.commit()
    audit.record(db, key.name, "module.bind", project.name,
                 {"module": module.name, "prefix": body.env_prefix})
    # 주입될 환경변수 키를 미리 보여준다 (값은 배포 시에만 주입)
    return {"injected_env": sorted(svc.binding_env(module, body.env_prefix, db=db).keys())}


@router.get("/modules/search")
def search_external_apis(
    keyword: str,
    _: ApiKey = Depends(require_admin),
):
    """키워드로 외부 API 디렉터리를 검색한다(요청 3). 아웃바운드 조회이므로 admin 전용.

    반환된 항목은 POST /modules/import로 external_api 모듈에 추가할 수 있다."""
    try:
        return {"results": apisearch.search_apis(keyword)}
    except apisearch.ApiSearchError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/modules/search/refresh")
def refresh_external_api_directory(
    _: ApiKey = Depends(require_admin),
):
    """외부 API 수집 루트(디렉터리)를 1일 1회 주기 외에 즉시 재탐색하고 업데이트한다."""
    try:
        data = apisearch.refresh_api_directory()
        return {"status": "success", "total_apis": len(data)}
    except apisearch.ApiSearchError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/modules/search/refresh-mcp")
def refresh_mcp_directory(
    _: ApiKey = Depends(require_admin),
):
    """외부 MCP 수집 루트를 1일 1회 주기 외에 즉시 재탐색하고 업데이트한다."""
    return mcp_search.refresh_mcp_directory()


@router.post("/modules/import", status_code=201)
def import_api_module(
    body: ApiModuleImport,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """검색 결과를 external_api 모듈로 자동 추가한다 — 이름은 모듈명 규약으로 정규화."""
    name = apisearch.normalize_module_name(body.name)
    base, suffix = name, 2
    while db.execute(select(Module).where(Module.name == name)).scalar_one_or_none():
        name = f"{base[:37]}-{suffix}"
        suffix += 1
    row = Module(
        name=name, type=ModuleType.external_api, category=body.category,
        config=svc.encrypt_config({"url": body.url}),
    )
    db.add(row)
    db.commit()
    audit.record(db, admin.name, "module.import", name, {"source": body.name, "url": body.url})
    return {"id": row.id, "name": row.name, "type": row.type.value, "category": row.category,
            "organization_id": row.organization_id, "config": svc.masked_config(row.config)}


@router.get("/projects/{project_id}/modules")
def project_modules(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return svc.context_for_llm(db, project)


@router.get("/projects/{project_id}/resources")
def project_resources(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """대화식 편집 화면용 — 바인딩 여부와 무관하게 이 프로젝트에서 쓸 수 있는 모든
    자원(카테고리별 API, 공유 파일 저장소, 조직별 DB 등)을 아이템화해 반환한다."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return svc.available_resources(db, project)


@router.get("/mcp/search")
def search_mcp_directory(q: str = "", _: ApiKey = Depends(require_api_key)):
    """외부 MCP 서버 디렉터리 키워드 검색."""
    return mcp_search.search_mcp_servers(q)


@router.post("/modules/import-mcp", status_code=201)
def import_mcp_module(
    body: ApiModuleImport,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """검색된 MCP 서버를 mcp 타입 모듈로 자동 추가한다."""
    mod_name = apisearch.normalize_module_name(body.name)
    if db.execute(select(Module).where(Module.name == mod_name)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"module '{mod_name}' already exists")

    config = {"url": body.url, "api_key": ""}
    row = Module(
        name=mod_name,
        type=ModuleType.mcp,
        category=body.category or "mcp",
        config=svc.encrypt_config(config),
    )
    db.add(row)
    db.commit()
    audit.record(db, admin.name, "module.import_mcp", mod_name, {"url": body.url})
    return {"id": row.id, "name": row.name, "type": row.type.value, "category": row.category,
            "config": svc.masked_config(row.config)}
