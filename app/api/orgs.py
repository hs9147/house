"""조직(Organization) 운영 — 조직별 작업공간을 만들고 사내 Gitea에 대응 Organization을
할당한다. 프로젝트별 리포 생성은 프로젝트 생성 시(api/projects.py) 플랫폼이 내부에서
처리하며, 사용자에게 git_url 등 메타 정보를 노출하지 않는다."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, Organization, Project
from ..schemas import GiteaSyncResult, OrgCreate, OrgOut
from ..security import require_admin, require_api_key
from ..services import gitea, gitea_sync
from ..services.gitea import GiteaError, GiteaNotConfigured

router = APIRouter(prefix="/orgs", tags=["organizations"])


@router.post("", response_model=OrgOut, status_code=201)
def create_org(
    body: OrgCreate,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    if db.execute(select(Organization).where(Organization.name == body.name)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="organization already exists")
    try:
        gitea.ensure_org(body.name)
    except GiteaNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except GiteaError as e:
        raise HTTPException(status_code=502, detail=str(e))

    org = Organization(name=body.name)
    db.add(org)
    db.commit()
    audit.record(db, admin.name, "org.create", org.name)
    return OrgOut(id=org.id, name=org.name, created_at=org.created_at, project_count=0)


@router.get("", response_model=list[OrgOut])
def list_orgs(db: Session = Depends(get_db), _: ApiKey = Depends(require_api_key)):
    rows = db.execute(
        select(Organization, func.count(Project.id))
        .outerjoin(Project, Project.organization_id == Organization.id)
        .group_by(Organization.id)
        .order_by(Organization.id)
    ).all()
    return [
        OrgOut(id=org.id, name=org.name, created_at=org.created_at, project_count=count)
        for org, count in rows
    ]


@router.post("/sync", response_model=GiteaSyncResult)
def sync_from_gitea(
    on_missing_repo: Literal["create", "delete"] = "create",
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """Gitea 기준으로 조직/프로젝트 현황을 동기화한다(관리자가 필요할 때 수동 트리거 —
    자동/주기 실행은 하지 않음). 두 방향: Gitea에는 있지만 플랫폼에 없는 조직/리포는
    가져오고, 플랫폼(조직 소속 프로젝트)에는 있지만 Gitea에 리포가 없는 경우는
    on_missing_repo에 따라 리포를 다시 만들거나("create", 기본값) 플랫폼 쪽
    프로젝트를 지운다("delete" — 되돌릴 수 없음)."""
    try:
        result = gitea_sync.sync_from_gitea(db, on_missing_repo)
    except GiteaNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except GiteaError as e:
        raise HTTPException(status_code=502, detail=str(e))
    audit.record(db, admin.name, "orgs.sync_from_gitea", "-", {
        "on_missing_repo": on_missing_repo,
        "orgs_created": len(result["orgs_created"]),
        "projects_created": len(result["projects_created"]),
        "repos_created": len(result["repos_created"]),
        "projects_deleted": result["projects_deleted"],
        "skipped": len(result["skipped"]),
    })
    return result
