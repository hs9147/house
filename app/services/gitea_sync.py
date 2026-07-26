"""Gitea 기준으로 조직·프로젝트 현황을 동기화한다 (관리자 수동 트리거, POST /orgs/sync).

두 방향을 다룬다:
1. Gitea → 플랫폼(가져오기): Gitea에는 있지만 플랫폼 DB에 없는 조직/리포를 찾아
   Organization/Project 행을 새로 만든다 — 기존 ensure_org/ensure_repo가 이미
   "플랫폼 → Gitea" 방향을 담당하고 있으므로, 이 부분은 그 반대 경로(수동으로
   Gitea에 만든 조직/리포를 플랫폼이 뒤늦게 인식)를 메운다.
2. 플랫폼에는 있지만 Gitea에 리포가 없는 경우(조직 소속 프로젝트 한정 — git_url을
   직접 지정한 레거시 프로젝트는 애초에 Gitea 관리 대상이 아니므로 제외): 관리자가
   on_missing_repo로 선택한 대로 리포를 Gitea에 다시 만들거나("create", 기본값)
   플랫폼 쪽 프로젝트를 지운다("delete").

리포의 Project.type은 얕은 clone으로 시그니처 파일을 확인해 추론한다
(services/build.py의 detect_project_type — detect_composite_components와 동일한
"추측성 기본값 금지" 원칙). 추론할 수 없거나 이름 규칙(^[a-z0-9][a-z0-9-]{1,40}$)에
안 맞는 리포는 만들지 않고 이유와 함께 건너뛴다.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    BuildProfile, ChatMessage, ChatSession, Deployment, EnvVar, ModuleBinding,
    Organization, PreviewSession, Project, ProjectType, ProposedChange, RedirectRule,
)
from . import gitea
from .build import COMPOSITE_COMPONENTS, detect_project_type
from .git_auth import auth_args
from .gitea import GiteaError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")


def sync_from_gitea(
    db: Session, on_missing_repo: Literal["create", "delete"] = "create",
) -> dict:
    orgs_created: list[str] = []
    projects_created: list[str] = []
    repos_created: list[str] = []
    projects_deleted: list[str] = []
    skipped: list[dict[str, str]] = []

    existing_org_names = {row[0] for row in db.execute(select(Organization.name)).all()}
    existing_project_names = {row[0] for row in db.execute(select(Project.name)).all()}
    gitea_repo_names_by_org: dict[str, set[str]] = {}

    for gitea_org in gitea.list_orgs():
        org_name = gitea_org["username"]
        if not NAME_RE.match(org_name):
            skipped.append({"name": org_name, "kind": "org", "reason": "이름 규칙에 맞지 않음"})
            continue

        if org_name in existing_org_names:
            org = db.execute(
                select(Organization).where(Organization.name == org_name)
            ).scalar_one()
        else:
            org = Organization(name=org_name)
            db.add(org)
            db.commit()
            db.refresh(org)
            existing_org_names.add(org_name)
            orgs_created.append(org_name)

        repos = gitea.list_org_repos(org_name)
        gitea_repo_names_by_org[org_name] = {r["name"] for r in repos}

        for repo in repos:
            repo_name = repo["name"]
            if not NAME_RE.match(repo_name):
                skipped.append({"name": repo_name, "kind": "project", "reason": "이름 규칙에 맞지 않음"})
                continue
            if repo_name in existing_project_names:
                continue  # 이미 등록됨(이 조직이거나, 이름이 겹치는 다른 조직 소속)

            branch = repo.get("default_branch") or "main"
            workdir = None
            try:
                workdir = _shallow_clone(repo["clone_url"], branch)
                ptype = detect_project_type(workdir)
            except RuntimeError as e:
                skipped.append({"name": repo_name, "kind": "project", "reason": f"clone 실패: {e}"})
                continue
            finally:
                if workdir is not None:
                    shutil.rmtree(workdir, ignore_errors=True)

            if ptype is None:
                skipped.append({
                    "name": repo_name, "kind": "project",
                    "reason": "타입을 추론할 수 없음 (시그니처 파일 없음)",
                })
                continue

            project = Project(
                name=repo_name, type=ptype, organization_id=org.id,
                git_url=repo["clone_url"], branch=branch,
            )
            db.add(project)
            db.commit()
            existing_project_names.add(repo_name)
            projects_created.append(repo_name)

    # 2) 플랫폼에는 있지만 Gitea에 리포가 없는 경우 — 조직 소속 프로젝트만 대상
    #    (git_url 직접 지정 레거시 프로젝트는 Gitea 관리 대상이 아니므로 제외).
    org_rows = db.execute(select(Organization)).scalars().all()
    for org in org_rows:
        if org.name not in gitea_repo_names_by_org:
            try:
                gitea_repo_names_by_org[org.name] = {r["name"] for r in gitea.list_org_repos(org.name)}
            except GiteaError:
                gitea_repo_names_by_org[org.name] = set()  # Gitea에 조직 자체가 없음 — 리포도 전부 없는 것으로 취급
        repo_names = gitea_repo_names_by_org[org.name]

        projects = db.execute(
            select(Project).where(Project.organization_id == org.id)
        ).scalars().all()
        for project in projects:
            if project.name in repo_names:
                continue
            if on_missing_repo == "delete":
                _delete_project(db, project)
                projects_deleted.append(project.name)
            else:
                clone_url = gitea.ensure_repo(org.name, project.name)
                project.git_url = clone_url
                db.commit()
                repos_created.append(project.name)

    return {
        "orgs_created": orgs_created,
        "projects_created": projects_created,
        "repos_created": repos_created,
        "projects_deleted": projects_deleted,
        "skipped": skipped,
    }


def _delete_project(db: Session, project: Project) -> None:
    """Gitea에 리포가 없는 프로젝트를 정리한다. 실행 중인 컨테이너를 멈추되(기존
    POST /projects/{id}/stop과 동일하게 런타임만 — 프록시 설정은 안 건드리는 기존
    관례를 그대로 따른다) 프로세스가 없으면 조용히 넘어가고, 딸린 행을 자식→부모
    순으로 지운 뒤 Project 자체를 지운다."""
    from .deployer import get_runtime  # noqa: PLC0415 — 순환 import 회피

    runtime = get_runtime()
    for profile in BuildProfile:
        try:
            if project.type == ProjectType.composite:
                for name in COMPOSITE_COMPONENTS:
                    runtime.stop(f"{project.name}-{name}", profile)
            else:
                runtime.stop(project.name, profile)
        except Exception:  # noqa: BLE001 — 런타임 미설치/미접근이 삭제 자체를 막지 않음
            pass

    session_ids = [
        row[0] for row in db.execute(
            select(ChatSession.id).where(ChatSession.project_id == project.id)
        ).all()
    ]
    if session_ids:
        db.execute(delete(ProposedChange).where(ProposedChange.session_id.in_(session_ids)))
        db.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(session_ids)))
        db.execute(delete(ChatSession).where(ChatSession.id.in_(session_ids)))
    db.execute(delete(Deployment).where(Deployment.project_id == project.id))
    db.execute(delete(EnvVar).where(EnvVar.project_id == project.id))
    db.execute(delete(ModuleBinding).where(ModuleBinding.project_id == project.id))
    db.execute(delete(RedirectRule).where(RedirectRule.project_id == project.id))
    db.execute(delete(PreviewSession).where(PreviewSession.project_id == project.id))
    db.delete(project)
    db.commit()


def _shallow_clone(git_url: str, branch: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="gitea-sync-"))
    proc = subprocess.run(
        ["git", *auth_args(git_url), "clone", "--depth", "1", "--branch", branch, git_url, str(tmp)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(proc.stderr.strip()[:300])
    return tmp
