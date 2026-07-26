from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, Module, ModuleBinding, ModuleType, Organization, Project
from ..schemas import ApiModuleImport, ModuleBind, ModuleCreate
from ..security import require_admin, require_api_key
from ..services import apisearch
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
    row = Module(
        name=body.name, type=ModuleType(body.type), category=body.category,
        organization_id=body.organization_id, config=svc.encrypt_config(body.config),
    )
    db.add(row)
    db.commit()
    audit.record(db, key.name, "module.create", body.name, {"type": body.type})
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
