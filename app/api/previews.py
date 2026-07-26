import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..db import get_db
from ..models import ApiKey, PreviewSession, PreviewStatus, Project
from ..schemas import PreviewCreate, PreviewOut
from ..security import require_api_key
from ..services import preview as svc

router = APIRouter(tags=["previews"])


@router.post("/projects/{project_id}/preview", response_model=PreviewOut)
async def create_preview(
    project_id: int,
    body: PreviewCreate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    branch = body.branch or project.branch
    try:
        record = await asyncio.to_thread(
            svc.create_preview_sync, db, project, branch, body.ttl_minutes
        )
    except svc.TooManyPreviews as e:
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:1000])
    audit.record(db, key.name, "preview.create", project.name,
                 {"branch": branch, "url": record.url, "ttl": body.ttl_minutes})
    return record


@router.get("/projects/{project_id}/previews", response_model=list[PreviewOut])
def list_previews(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    svc.cleanup_expired(db, project)  # 접근 시점 lazy 회수
    return (
        db.execute(
            select(PreviewSession)
            .where(PreviewSession.project_id == project_id)
            .order_by(PreviewSession.id.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )


@router.delete("/previews/{preview_id}", status_code=204)
def delete_preview(
    preview_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    ps = db.get(PreviewSession, preview_id)
    if ps is None:
        raise HTTPException(status_code=404, detail="preview not found")
    project = db.get(Project, ps.project_id)
    if ps.status == PreviewStatus.running:
        svc.teardown(db, ps, project)
    audit.record(db, key.name, "preview.delete", project.name, {"preview_id": preview_id})
