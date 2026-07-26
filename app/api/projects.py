import shutil

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..features import is_enabled, require_feature
from ..git_policy import enforce_internal_git_url
from ..models import ApiKey, BuildProfile, Deployment, EnvVar, Organization, Project, ProjectType
from ..schemas import (
    DeploymentOut,
    DeployRequest,
    EnvVarSet,
    ProjectCreate,
    ProjectOut,
    ProjectUploadForm,
)
from ..security import encrypt_value, require_api_key
from ..services import deployer, gitea, upload
from ..services.build import COMPOSITE_COMPONENTS
from ..services.deployer import DeployInProgress, NoRollbackTarget
from ..services.gitea import GiteaError, GiteaNotConfigured
from ..services.upload import UploadError, UploadRejected

router = APIRouter(prefix="/projects", tags=["projects"])

GIT_URL_MASK = "(내부 관리 — 관리자만 조회 가능)"


def _serialize_project(project: Project, key: ApiKey) -> ProjectOut:
    """비관리자에게는 git_url(리포 위치) 등 메타 정보를 노출하지 않는다."""
    out = ProjectOut.model_validate(project)
    if not key.is_admin:
        out.git_url = GIT_URL_MASK
    return out


def _get_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db), key: ApiKey = Depends(require_api_key)):
    rows = db.execute(select(Project).order_by(Project.id)).scalars()
    return [_serialize_project(p, key) for p in rows]


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    exists = db.execute(select(Project).where(Project.name == body.name)).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="project name already exists")

    git_url = body.git_url
    if body.organization_id is not None:
        org = db.get(Organization, body.organization_id)
        if org is None:
            raise HTTPException(status_code=404, detail="organization not found")
        try:
            # 프로젝트별 레포 생성·코드 관리는 플랫폼이 내부에서 처리 — 사용자는
            # git_url을 직접 지정하거나 조회하지 않는다.
            git_url = gitea.ensure_repo(org.name, body.name)
        except GiteaNotConfigured as e:
            raise HTTPException(status_code=503, detail=str(e))
        except GiteaError as e:
            raise HTTPException(status_code=502, detail=str(e))
        try:
            # 자동 배포를 위한 웹훅 등록은 베스트 에포트 — 실패해도 프로젝트
            # 생성 자체는 막지 않는다(infra/gitea/README.md 수동 절차로 대체 가능).
            gitea.ensure_webhook(org.name, body.name)
        except GiteaError:
            pass

    enforce_internal_git_url(git_url)
    data = body.model_dump(exclude={"git_url"})
    project = Project(**data, git_url=git_url)
    db.add(project)
    db.commit()
    audit.record(db, key.name, "project.create", project.name)
    return _serialize_project(project, key)


@router.post("/upload", response_model=ProjectOut, status_code=201)
async def upload_project(
    name: str = Form(..., pattern=r"^[a-z0-9][a-z0-9-]{1,40}$"),
    type: ProjectType = Form(...),  # noqa: A002 - Form 필드명을 ProjectCreate와 맞춤
    organization_id: int = Form(...),
    branch: str = Form("main"),
    domain: str | None = Form(None),
    health_check_path: str = Form("/"),
    default_profile: BuildProfile = Form(BuildProfile.release),
    deploy_after_upload: bool = Form(False),
    zip_file: UploadFile | None = File(default=None),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """zip 또는 폴더(다중 파일) 업로드로 프로젝트를 등록한다.

    조직 소속 사내 Gitea 리포를 새로 만들어 업로드 내용을 최초 커밋으로 push한다
    (레거시 git_url 직접 지정 경로는 없음 — 소스가 사외로 나가지 않는다는 보장과
    동일 원칙). 대용량·악성 업로드는 services/upload.py에서 방어한다.

    (multipart 요청에서 pydantic 모델을 File 파라미터와 함께 Form()으로 받으면
    FastAPI가 "form" 키로 재감싸는 동작이 있어, 개별 Form 필드로 받은 뒤 여기서
    ProjectUploadForm으로 재검증한다.)
    """
    form = ProjectUploadForm(
        name=name, type=type, organization_id=organization_id, branch=branch,
        domain=domain, health_check_path=health_check_path,
        default_profile=default_profile, deploy_after_upload=deploy_after_upload,
    )
    exists = db.execute(select(Project).where(Project.name == form.name)).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="project name already exists")
    org = db.get(Organization, form.organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="organization not found")

    has_zip = zip_file is not None and bool(zip_file.filename)
    has_folder = len(files) > 0
    if has_zip == has_folder:  # 둘 다 없거나 둘 다 있으면 오류
        raise HTTPException(
            status_code=422, detail="zip_file 또는 files 중 정확히 하나를 업로드해야 합니다"
        )

    settings = get_settings()
    workdir = settings.work_dir / form.name
    shutil.rmtree(workdir, ignore_errors=True)

    try:
        if has_zip:
            data = await upload.read_capped(zip_file, settings.upload_max_zip_mb * 1024 * 1024)
            upload.stage_zip(data, workdir)
        else:
            await upload.stage_folder(files, workdir)

        try:
            git_url = gitea.ensure_repo(org.name, form.name, auto_init=False)
        except GiteaNotConfigured as e:
            raise HTTPException(status_code=503, detail=str(e))
        except GiteaError as e:
            raise HTTPException(status_code=502, detail=str(e))

        enforce_internal_git_url(git_url)

        try:
            git_sha = upload.init_repo_and_push(workdir, git_url, form.branch)
        except UploadError as e:
            raise HTTPException(status_code=502, detail=str(e))

        try:
            gitea.ensure_webhook(org.name, form.name)
        except GiteaError:
            pass  # 웹훅 자동 등록은 베스트 에포트 — 실패해도 업로드 자체는 성공 처리
    except UploadRejected as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        raise

    project = Project(
        name=form.name,
        type=form.type,
        organization_id=form.organization_id,
        git_url=git_url,
        branch=form.branch,
        domain=form.domain,
        health_check_path=form.health_check_path,
        default_profile=form.default_profile,
    )
    db.add(project)
    db.commit()
    audit.record(db, key.name, "project.upload", project.name, {"git_sha": git_sha})

    if form.deploy_after_upload and is_enabled("deploy"):
        if project.type == ProjectType.composite:
            deployer.deploy_composite_queued(db, project, form.default_profile, git_sha)
        else:
            deployer.deploy_queued(db, project, form.default_profile, git_sha)

    return _serialize_project(project, key)


@router.post("/{project_id}/deploy", response_model=DeploymentOut | list[DeploymentOut],
             dependencies=[Depends(require_feature("deploy"))])
async def deploy_project(
    project_id: int,
    body: DeployRequest,
    response: Response,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = _get_project(db, project_id)
    profile = body.profile or project.default_profile
    if project.type == ProjectType.composite:
        if not body.wait:
            records = deployer.deploy_composite_queued(db, project, profile, body.git_sha)
            response.status_code = 202
            audit.record(db, key.name, "deploy.queued", project.name,
                         {"profile": profile.value, "deployment_ids": [r.id for r in records.values()]})
            return list(records.values())
        try:
            records = await deployer.deploy_composite(db, project, profile, body.git_sha)
        except DeployInProgress as e:
            raise HTTPException(status_code=409, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e)[:1000])
        audit.record(
            db, key.name, "deploy", project.name,
            {"profile": profile.value, "deployment_ids": [r.id for r in records.values()]},
        )
        return list(records.values())
    if not body.wait:
        record = deployer.deploy_queued(db, project, profile, body.git_sha)
        response.status_code = 202
        audit.record(db, key.name, "deploy.queued", project.name,
                     {"profile": profile.value, "deployment_id": record.id})
        return record
    try:
        record = await deployer.deploy(db, project, profile, body.git_sha)
    except DeployInProgress as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:1000])
    audit.record(
        db, key.name, "deploy", project.name,
        {"profile": profile.value, "sha": record.git_sha, "deployment_id": record.id},
    )
    return record


@router.post("/{project_id}/rollback", response_model=DeploymentOut | list[DeploymentOut],
             dependencies=[Depends(require_feature("deploy"))])
def rollback_project(
    project_id: int,
    profile: BuildProfile = BuildProfile.release,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = _get_project(db, project_id)
    if project.type == ProjectType.composite:
        try:
            records = deployer.rollback_composite(db, project, profile)
        except NoRollbackTarget as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e)[:1000])
        audit.record(db, key.name, "rollback", project.name, {
            "profile": profile.value,
            "to_shas": {name: r.git_sha for name, r in records.items()},
        })
        return list(records.values())
    try:
        record = deployer.rollback(db, project, profile)
    except NoRollbackTarget as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)[:1000])
    audit.record(db, key.name, "rollback", project.name,
                 {"profile": profile.value, "to_sha": record.git_sha})
    return record


@router.post("/{project_id}/stop", status_code=204,
             dependencies=[Depends(require_feature("deploy"))])
def stop_project(
    project_id: int,
    profile: BuildProfile = BuildProfile.release,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = _get_project(db, project_id)
    runtime = deployer.get_runtime()
    if project.type == ProjectType.composite:
        for name in COMPOSITE_COMPONENTS:
            runtime.stop(f"{project.name}-{name}", profile)
    else:
        runtime.stop(project.name, profile)
    audit.record(db, key.name, "stop", project.name, {"profile": profile.value})


@router.get("/{project_id}/deployments", response_model=list[DeploymentOut],
            dependencies=[Depends(require_feature("deploy"))])
def list_deployments(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    _get_project(db, project_id)
    return (
        db.execute(
            select(Deployment)
            .where(Deployment.project_id == project_id)
            .order_by(Deployment.id.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )


@router.get("/{project_id}/logs", dependencies=[Depends(require_feature("deploy"))])
def project_logs(
    project_id: int,
    profile: BuildProfile = BuildProfile.release,
    tail: int = 200,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    project = _get_project(db, project_id)
    runtime = deployer.get_runtime()
    if project.type == ProjectType.composite:
        return {
            name: runtime.logs(f"{project.name}-{name}", profile, tail)
            for name in COMPOSITE_COMPONENTS
        }
    return {"logs": runtime.logs(project.name, profile, tail)}


@router.get("/{project_id}/status", dependencies=[Depends(require_feature("deploy"))])
def project_status(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    project = _get_project(db, project_id)
    runtime = deployer.get_runtime()
    if project.type == ProjectType.composite:
        return {
            profile.value: {
                name: runtime.status(f"{project.name}-{name}", profile)
                for name in COMPOSITE_COMPONENTS
            }
            for profile in BuildProfile
        }
    return {
        profile.value: runtime.status(project.name, profile)
        for profile in BuildProfile
    }


@router.put("/{project_id}/env", status_code=204)
def set_env_var(
    project_id: int,
    body: EnvVarSet,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    project = _get_project(db, project_id)
    row = db.execute(
        select(EnvVar).where(EnvVar.project_id == project_id, EnvVar.key == body.key)
    ).scalar_one_or_none()
    if row is None:
        row = EnvVar(project_id=project_id, key=body.key)
        db.add(row)
    row.value_encrypted = encrypt_value(body.value)
    row.is_secret = body.is_secret
    db.commit()
    # 값은 감사 로그에도 남기지 않는다
    audit.record(db, key.name, "env.set", project.name, {"key": body.key})


@router.get("/{project_id}/env")
def list_env_vars(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    _get_project(db, project_id)
    rows = db.execute(select(EnvVar).where(EnvVar.project_id == project_id)).scalars()
    # 시크릿 값은 마스킹해서만 노출
    return [{"key": r.key, "is_secret": r.is_secret, "value": "•••" if r.is_secret else "(set)"}
            for r in rows]
