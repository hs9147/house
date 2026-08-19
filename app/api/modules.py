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
from ..schemas import (
    ApiModuleImport,
    GlobalModuleUsageSummary,
    ModuleBind,
    ModuleCreate,
    ModuleHistoryItem,
    PlatformModuleReportOut,
)
from ..security import require_admin, require_api_key
from ..services import a2a as a2a_service
from ..services import apisearch
from ..services import mcp_client, mcp_search
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
    
    # file_storage 일 경우 PAAS_STORAGE_ROOT 환경변수를 적용하고 폴더를 자동 생성
    if body.type == ModuleType.file_storage.value:
        settings = get_settings()
        env_storage_root = str(Path(settings.storage_root or "./data/storage").resolve())
        if not body.config:
            body.config = {}
        # endpoint가 비어있거나 기본값일 경우 PAAS_STORAGE_ROOT 환경변수로 자동 보정
        if not body.config.get("endpoint") or body.config.get("endpoint") in ["./data/storage", "data/storage"]:
            body.config["endpoint"] = env_storage_root
        body.config["storage_root"] = env_storage_root

        sub_folder = body.config.get("sub_folder") or body.config.get("bucket")
        if sub_folder:
            root_path = Path(body.config.get("endpoint") or env_storage_root).resolve()
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

    config = {
        "endpoint": str(root_path),
        "storage_root": str(root_path),
        "sub_folder": folder_name,
        "bucket": folder_name,
    }
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


@router.delete("/projects/{project_id}/modules/bindings/{binding_id}", status_code=204)
def unbind_module(
    project_id: int,
    binding_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """프로젝트에서 모듈 바인딩을 해제한다. 모듈 정의 자체는 남고, 다음 배포부터
    이 바인딩의 환경변수만 더는 주입되지 않는다."""
    binding = db.get(ModuleBinding, binding_id)
    if binding is None or binding.project_id != project_id:
        raise HTTPException(status_code=404, detail="binding not found")
    project = db.get(Project, project_id)
    module = db.get(Module, binding.module_id)
    db.delete(binding)
    db.commit()
    audit.record(db, key.name, "module.unbind", project.name if project else str(project_id),
                 {"module": module.name if module else str(binding.module_id),
                  "prefix": binding.env_prefix})
    return None


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
    return a2a_service.list_project_a2a_cards(db, project)


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


@router.post("/modules/{module_id}/mcp-check")
def check_mcp_module(
    module_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_admin),
):
    """이 MCP 모듈이 실제로 응답하는지 확인한다(tools/list 1회).

    등록만으로는 동작을 알 수 없다 — 주소가 틀렸거나 전송 방식이 안 맞으면 등록은
    성공한 채 조용히 죽어 있다. 확인 실패는 오류가 아니라 결과이므로 200으로 내려주고
    본문의 ok/error로 구분한다(화면이 여러 모듈을 나열하며 표시한다).
    """
    row = db.get(Module, module_id)
    if row is None:
        raise HTTPException(status_code=404, detail="module not found")
    if row.type != ModuleType.mcp:
        raise HTTPException(status_code=400, detail=f"mcp 타입 모듈이 아닙니다: {row.type.value}")
    config = svc.decrypt_config(row.config)
    return {
        "module_id": row.id,
        "name": row.name,
        "url": config.get("url", ""),
        **mcp_client.check_server(config.get("url", ""), config.get("api_key") or None),
    }


@router.get("/modules/usage-report", response_model=PlatformModuleReportOut)
def get_platform_module_report(
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """PaaS 플랫폼 전역 모듈 사용 이력 리포트 — 등록된 모든 모듈의 바인딩 프로젝트 현황 및 최근 모듈 관련 변경 로그를 종합 집계한다."""
    from sqlalchemy.orm import joinedload  # noqa: PLC0415
    from ..models import AuditEvent  # noqa: PLC0415

    modules = db.execute(
        select(Module).options(joinedload(Module.organization)).order_by(Module.id.desc())
    ).scalars().unique().all()

    bindings = db.execute(
        select(ModuleBinding, Project)
        .join(Project, ModuleBinding.project_id == Project.id)
    ).all()

    bindings_by_module: dict[int, list[str]] = {}
    for binding, proj in bindings:
        bindings_by_module.setdefault(binding.module_id, []).append(proj.name)

    summaries: list[GlobalModuleUsageSummary] = []
    total_bindings = len(bindings)

    for m in modules:
        proj_list = bindings_by_module.get(m.id, [])
        org_name = m.organization.name if m.organization else None
        summaries.append(
            GlobalModuleUsageSummary(
                module_id=m.id,
                module_name=m.name,
                type=m.type.value,
                category=m.category,
                organization_name=org_name,
                bound_project_count=len(proj_list),
                bound_projects=proj_list,
                created_at=m.created_at,
            )
        )

    # 최근 모듈 관련 감사 이벤트
    audit_rows = db.execute(
        select(AuditEvent)
        .where(AuditEvent.action.like("module.%"))
        .order_by(AuditEvent.created_at.desc())
        .limit(100)
    ).scalars()

    recent_history: list[ModuleHistoryItem] = [
        ModuleHistoryItem(
            id=r.id,
            actor=r.actor,
            action=r.action,
            target=r.target,
            payload=r.payload or {},
            created_at=r.created_at,
        )
        for r in audit_rows
    ]

    return PlatformModuleReportOut(
        total_modules=len(modules),
        total_bindings=total_bindings,
        modules=summaries,
        recent_history=recent_history,
    )
