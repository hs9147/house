"""공용 DB 창구 — 과제의 스키마, 읽을 수 있는 표, 표 단위 권한(services/shareddb).

권한을 주고 거두는 것은 **플랫폼 관리자만** 한다(조직 관리자는 나중에 — 같은 창구에 조직 범위만
더하면 된다).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, DataGrant, DataSchema, Module, ModuleBinding, Project
from ..security import require_admin, require_api_key
from ..services import shareddb

router = APIRouter(tags=["shared-db"])


def _grant_out(g: DataGrant, names: dict[int, str]) -> dict:
    return {"id": g.id, "schema": g.schema_name, "table": g.table_name, "project_id": g.project_id,
            "project_name": names.get(g.project_id, ""), "privilege": g.privilege,
            "expires_at": g.expires_at.isoformat() if g.expires_at else None,
            "granted_by": g.granted_by, "created_at": g.created_at.isoformat()}


def _names(db: Session) -> dict[int, str]:
    return {p.id: p.name for p in db.execute(select(Project)).scalars()}


@router.get("/shared-db")
def shared_db_status(db: Session = Depends(get_db), _: ApiKey = Depends(require_admin)):
    """플랫폼이 만든 스키마 전부(지운 과제의 것도)와 표 권한."""
    names = _names(db)
    return {"configured": shareddb.configured(), "schemas": shareddb.listing(db),
            "grants": [_grant_out(g, names) for g in db.execute(select(DataGrant)).scalars()]}


@router.get("/projects/{project_id}/shared-db")
def project_shared_db(project_id: int, db: Session = Depends(get_db),
                      _: ApiKey = Depends(require_api_key)):
    """이 과제의 공용 DB — 바인딩 여부, 환경변수 이름, 스키마, 읽을 수 있는 표, 준·받은 권한.

    표 목록은 DB에서 읽는다 — 서버가 내려가 있으면 error에 사유를 싣고 나머지는 그대로 준다.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    binding = db.execute(
        select(ModuleBinding).join(Module, ModuleBinding.module_id == Module.id)
        .where(ModuleBinding.project_id == project_id, Module.name == shareddb.MODULE_NAME)
    ).scalar_one_or_none()
    schemas = db.execute(select(DataSchema).where(DataSchema.project_id == project_id)
                         .order_by(DataSchema.profile)).scalars().all()
    names = _names(db)
    own = {s.schema_name for s in schemas}
    out = {
        "configured": shareddb.configured(),
        "bound": binding is not None,
        "env_key": f"{binding.env_prefix.upper()}_DSN" if binding else "",
        "schemas": [{"profile": s.profile, "schema": s.schema_name, "db_name": s.db_name}
                    for s in schemas],
        "tables": {"own": [], "unit": [], "granted": [], "requestable": []},
        "given": [_grant_out(g, names) for g in db.execute(
            select(DataGrant).where(DataGrant.schema_name.in_(own))).scalars()] if own else [],
        "error": "",
    }
    if shareddb.configured() and schemas:
        try:
            out["tables"] = shareddb.readable(db, project)
        except shareddb.SharedDbError as e:
            out["error"] = str(e)
    return out


@router.get("/shared-db/policies")
def list_policies(db: Session = Depends(get_db), _: ApiKey = Depends(require_admin)):
    """부서마다 공유 단위·분리·DB·스키마 수(관리자). 공용 DB를 쓰지 않는 설치본이면 화면이 숨는다."""
    return {"configured": shareddb.configured(), "policies": shareddb.policy_rows(db)}


class PolicyIn(BaseModel):
    share_with_org_id: int | None = None  # 비우면 자기 부서가 공유 단위
    isolated: bool = False                # 공유 단위만 — 켜면 그 단위만 자기 DB를 쓴다


@router.put("/shared-db/policies/{org_id}")
def put_policy(org_id: int, body: PolicyIn, db: Session = Depends(get_db),
               admin: ApiKey = Depends(require_admin)):
    """부서의 공유 단위를 정한다(관리자). 자료가 있는 부서의 DB가 바뀌는 설정은 409로 거부한다."""
    try:
        rows = shareddb.set_policy(db, org_id, body.share_with_org_id, body.isolated)
    except shareddb.SharedDbError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit.record(db, admin.name, "shared_db.policy", str(org_id),
                 {"share_with_org_id": body.share_with_org_id, "isolated": body.isolated})
    return rows


class GrantIn(BaseModel):
    schema_name: str
    table: str
    project_id: int
    privilege: str = "read"
    expires_at: datetime | None = None


@router.post("/shared-db/grants", status_code=201)
def create_grant(body: GrantIn, db: Session = Depends(get_db), admin: ApiKey = Depends(require_admin)):
    """다른 과제에 표 하나를 읽기/쓰기로 준다(관리자) — 바로 DB에 반영한다."""
    project = db.get(Project, body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    try:
        row = shareddb.grant(db, body.schema_name, body.table, project, body.privilege,
                             admin.name, body.expires_at)
    except shareddb.SharedDbError as e:
        raise HTTPException(status_code=409, detail=str(e))
    audit.record(db, admin.name, "shared_db.grant", f"{body.schema_name}.{body.table}",
                 {"project": project.name, "privilege": body.privilege,
                  "expires_at": body.expires_at.isoformat() if body.expires_at else None})
    return _grant_out(row, _names(db))


@router.delete("/shared-db/grants/{grant_id}", status_code=204)
def delete_grant(grant_id: int, db: Session = Depends(get_db), admin: ApiKey = Depends(require_admin)):
    row = db.get(DataGrant, grant_id)
    target = f"{row.schema_name}.{row.table_name}" if row else str(grant_id)
    try:
        shareddb.revoke(db, grant_id)
    except shareddb.SharedDbError as e:
        raise HTTPException(status_code=404, detail=str(e))
    audit.record(db, admin.name, "shared_db.revoke", target, {"grant_id": grant_id})
