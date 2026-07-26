"""GitHub / Gitea push 웹훅 → 자동 배포 (프로젝트의 default_profile 사용)."""
import json

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from sqlalchemy import select

from .. import audit
from ..config import get_settings
from ..db import SessionLocal
from ..models import Project, ProjectType
from ..security import verify_webhook_signature
from ..services import deployer

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/git")
async def git_push(
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: str = Header(default=""),
    x_gitea_signature: str = Header(default=""),
):
    settings = get_settings()
    body = await request.body()
    signature = x_hub_signature_256 or x_gitea_signature
    if not verify_webhook_signature(settings.webhook_secret, body, signature):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    payload = json.loads(body)
    repo_urls = _repo_urls(payload)
    branch = (payload.get("ref") or "").removeprefix("refs/heads/")
    if not branch:
        return {"skipped": "no branch ref"}

    with SessionLocal() as db:
        projects = db.execute(select(Project)).scalars().all()
        matched = [
            p for p in projects
            if p.branch == branch and _normalize(p.git_url) in repo_urls
        ]
        if not matched:
            return {"skipped": f"no project for {branch}"}
        for project in matched:
            audit.record(db, "webhook", "deploy.trigger", project.name, {"branch": branch})
            background.add_task(_deploy_task, project.id)
    return {"triggered": [p.name for p in matched]}


def _deploy_task(project_id: int) -> None:
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is None:
            return
        try:
            if project.type == ProjectType.composite:
                deployer.deploy_composite_sync(db, project, project.default_profile)
            else:
                deployer.deploy_sync(db, project, project.default_profile)
        except deployer.DeployInProgress:
            # 연속 push: 진행 중 배포가 최신 커밋을 집도록 두고 이번 이벤트는 스킵
            pass
        except Exception as e:
            audit.record(db, "webhook", "deploy.failed", project.name, {"error": str(e)[:500]})


def _repo_urls(payload: dict) -> set[str]:
    repo = payload.get("repository") or {}
    urls = {repo.get("clone_url"), repo.get("ssh_url"), repo.get("html_url"), repo.get("url")}
    return {_normalize(u) for u in urls if u}


def _normalize(url: str) -> str:
    url = url.strip().removesuffix(".git").rstrip("/")
    # git@host:owner/repo → host/owner/repo 로 통일
    if url.startswith("git@"):
        url = url[len("git@"):].replace(":", "/", 1)
    for prefix in ("https://", "http://", "ssh://git@", "ssh://"):
        url = url.removeprefix(prefix)
    return url.lower()
