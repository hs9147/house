"""공용 DB 창구 — 플랫폼이 만든 DB 목록과 공용 자료 쓰기 권한(services/shareddb)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, Module, ModuleBinding, Project, SharedDatabase
from ..security import require_admin, require_api_key
from ..services import shareddb

router = APIRouter(tags=["shared-db"])


@router.get("/shared-db")
def shared_db_status(db: Session = Depends(get_db), _: ApiKey = Depends(require_admin)):
    """플랫폼이 만든 DB 전부 — 지운 프로젝트의 DB(orphan)도 남아 있는 한 보인다."""
    return {"configured": shareddb.configured(), "databases": shareddb.listing(db)}


@router.get("/projects/{project_id}/shared-db")
def project_shared_db(project_id: int, db: Session = Depends(get_db),
                      _: ApiKey = Depends(require_api_key)):
    """이 프로젝트의 공용 DB — 바인딩 여부, 앱이 받을 환경변수 이름, 만들어진 DB(비밀번호 없음)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    binding = db.execute(
        select(ModuleBinding).join(Module, ModuleBinding.module_id == Module.id)
        .where(ModuleBinding.project_id == project_id, Module.name == shareddb.MODULE_NAME)
    ).scalar_one_or_none()
    rows = db.execute(select(SharedDatabase).where(SharedDatabase.project_id == project_id)
                      .order_by(SharedDatabase.profile)).scalars()
    return {
        "configured": shareddb.configured(),
        "bound": binding is not None,
        "env_key": f"{binding.env_prefix.upper()}_DSN" if binding else "",
        "common_db": shareddb.COMMON_DB,
        "databases": [{"profile": r.profile, "db_name": r.db_name, "common_write": r.common_write,
                       "created_at": r.created_at.isoformat()} for r in rows],
    }


class CommonWriteIn(BaseModel):
    on: bool


@router.post("/projects/{project_id}/shared-db/common-write")
def set_common_write(project_id: int, body: CommonWriteIn, db: Session = Depends(get_db),
                     admin: ApiKey = Depends(require_admin)):
    """공용 자료 DB에 쓰기를 주거나 거둔다 — 운영 DB 계정에만(관리자). 기본은 읽기만이다."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        shareddb.set_common_writer(db, project, body.on)
    except shareddb.SharedDbError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit.record(db, admin.name, "shared_db.common_write", project.name, {"on": body.on})
    return project_shared_db(project_id, db, admin)
