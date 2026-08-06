"""GitHub / Gitea push 웹훅 → 자동 배포 (프로젝트의 default_profile 사용)."""
import json

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from sqlalchemy import select

from .. import audit
from ..config import get_settings
from ..db import SessionLocal
from ..models import Deployment, DeploymentStatus, Project, ProjectType
from ..security import verify_webhook_signature
from ..services import compliance as compliance_service
from ..services import deployer
from ..services import gitea
from ..services import planning as planning_service
from ..services import workspace

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
        known = [p for p in projects if _normalize(p.git_url) in repo_urls]
        if not known:
            return {"skipped": f"no project for {branch}"}

        # 기본 브랜치가 아닌 push는 외주 빌더가 올린 작업 브랜치다 — 사내 Gitea라면
        # 기본 브랜치로 가는 PR을 자동으로 만들어 둔다. 머지는 사람이 한다.
        # 결과가 기본 브랜치에 닿을 길이 없으면 작업 지시 진행 현황(기본 브랜치 기준)이
        # 영원히 갱신되지 않는다.
        pulls = [p.name for p in known if p.branch != branch]
        for project in known:
            if project.branch != branch:
                background.add_task(_pull_request_task, project.id, branch)

        # 기획 산출물만 바뀐 push는 배포 신호가 아니다 — 단계 확정 커밋이 기본 브랜치로
        # 머지될 때마다 운영본이 재배포되는 것을 막는다(변경 경로를 모르면 기존대로 배포).
        if _only_plan_artifacts(payload):
            return {"skipped": "plan artifacts only", "pull_requests": pulls}

        # Gitea가 같은 push를 재전달하면(네트워크 재시도, 관리자의 수동 "Redeliver" 등)
        # 이 커밋이 이미 배포 완료(running) 또는 배포 중(building)인지 확인해 중복
        # 빌드·재기동을 건너뛴다 — after(결과 커밋 SHA)가 없는 payload는 판단하지
        # 않고 그대로 트리거한다(배포를 놓치는 것보다 한 번 더 하는 편이 안전).
        after_sha = payload.get("after") or ""
        matched = [p for p in known if p.branch == branch]
        triggered = []
        skipped_duplicate = []
        for project in matched:
            if after_sha and _already_deployed(db, project, after_sha):
                skipped_duplicate.append(project.name)
                continue
            audit.record(db, "webhook", "deploy.trigger", project.name, {"branch": branch})
            background.add_task(_deploy_task, project.id)
            background.add_task(_compliance_task, project.id)
            triggered.append(project.name)
    return {"triggered": triggered, "pull_requests": pulls, "skipped_duplicate": skipped_duplicate}


def _already_deployed(db, project: Project, sha: str) -> bool:
    """이 커밋으로 이미 배포 완료(running)됐거나 지금 배포 중(building)이면 True.

    최신 배포 레코드 하나만 보면 충분하다 — 다음 커밋이 오면 자연히 최신이 그걸로
    바뀌므로, 과거에 이 sha로 배포했던 적이 있다는 사실만으로는(예: 되돌린 커밋을
    다시 push) 건너뛰지 않는다. composite 프로젝트도 두 컴포넌트 레코드가 같은
    sha로 한꺼번에 커밋되므로 가장 최근 레코드 하나로 충분하다."""
    latest = db.execute(
        select(Deployment)
        .where(Deployment.project_id == project.id)
        .order_by(Deployment.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return bool(
        latest and latest.git_sha == sha
        and latest.status in (DeploymentStatus.running, DeploymentStatus.building)
    )


def _pull_request_task(project_id: int, branch: str) -> None:
    """작업 브랜치 → 기본 브랜치 PR을 만든다(사내 Gitea 리포 한정).

    이미 열린 PR이 있으면 그것을 재사용하고(멱등), 반영할 커밋이 없으면 조용히 지나간다.
    자동 머지는 하지 않는다 — 외주 결과는 사람이 보고 머지한다.
    """
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is None:
            return
        slug = gitea.repo_slug(project.git_url)
        if slug is None:
            return  # 사내 Gitea가 아니면 API로 PR을 만들 수 없다
        owner, repo = slug
        try:
            pr = gitea.ensure_pull_request(
                owner, repo, branch, project.branch,
                f"build: {branch} → {project.branch}",
                "외주 빌드 결과 반영 PR (플랫폼 자동 생성). 검토 후 머지하면 "
                "작업 지시 진행 현황이 완료로 갱신됩니다.",
            )
        except gitea.GiteaNothingToMerge:
            return  # 기본 브랜치에 이미 반영됨 — 만들 PR이 없다
        except gitea.GiteaError as e:  # 설정 누락·API 실패가 배포를 건드리면 안 된다
            audit.record(db, "webhook", "plan.build.pull_request.failed", project.name,
                         {"branch": branch, "error": str(e)[:500]})
            return
        audit.record(db, "webhook", "plan.build.pull_request", project.name,
                     {"branch": branch, "number": pr.get("number"), "url": pr.get("html_url")})


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


def _compliance_task(project_id: int) -> None:
    """외주 빌더가 push한 코드의 LLM·모듈 사용을 검사해 경고로 남긴다.

    **막지 않는다** — 위반이 있어도 머지·배포는 그대로 진행하고 경고만 기록한다
    (docs/agent-planning/05-운영검토목록.md §2 결정). 빌더는 MCP check_compliance로
    전문 수정 지시를 받아 스스로 고친다.
    """
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if project is None:
            return
        try:
            constraints = planning_service.build_constraints(db, project)
            findings = compliance_service.scan(workspace.workdir_for(project), constraints)
        except Exception as e:  # noqa: BLE001 — 검사 실패가 배포를 건드리면 안 된다
            audit.record(db, "webhook", "plan.build.compliance.failed", project.name,
                         {"error": str(e)[:500]})
            return
        if not findings:
            return
        audit.record(db, "webhook", "plan.build.compliance", project.name, {
            "status": "warning",
            "summary": compliance_service.summarize(findings),
            "findings": [{"rule": f["rule"], "file": f["file"], "line": f["line"]}
                         for f in findings[:20]],
        })


def _only_plan_artifacts(payload: dict) -> bool:
    """이번 push의 변경 경로가 전부 기획 산출물 디렉터리 안인지.

    변경 목록이 없는 payload(형식이 다르거나 커밋이 생략된 경우)는 판단하지 않는다 —
    배포를 놓치는 것보다 한 번 더 배포하는 편이 안전하다.
    """
    prefix = planning_service.ARTIFACT_DIR + "/"
    paths: list[str] = []
    for commit in payload.get("commits") or []:
        for field in ("added", "removed", "modified"):
            paths.extend(commit.get(field) or [])
    return bool(paths) and all(p.startswith(prefix) for p in paths)


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
