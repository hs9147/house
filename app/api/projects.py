import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .. import audit
from ..config import get_settings
from ..db import get_db
from ..features import is_enabled, require_feature
from ..git_policy import enforce_internal_git_url
from ..models import (
    ApiKey,
    AuditEvent,
    BuildProfile,
    ChatMessage,
    ChatSession,
    Deployment,
    DeploymentStatus,
    EnvVar,
    Module,
    ModuleBinding,
    Organization,
    PlanArtifact,
    PortAllocation,
    PreviewSession,
    Project,
    ProjectType,
    RedirectRule,
)
from ..schemas import (
    DeploymentOut,
    DeployRequest,
    EnvVarSet,
    ModuleHistoryItem,
    ModuleUsageItem,
    ProjectCreate,
    ProjectModuleReportOut,
    ProjectOut,
    ProjectUploadForm,
)
from ..security import can_view_git_url, encrypt_value, require_admin, require_api_key, viewer_org_ids
from ..services import deployer, gitea, upload
from ..services.build import COMPOSITE_COMPONENTS
from ..services.deployer import DeployInProgress, NoRollbackTarget, ProfileConflict
from ..services.gitea import GiteaError, GiteaNotConfigured
from ..services.upload import UploadError, UploadRejected

router = APIRouter(prefix="/projects", tags=["projects"])

GIT_URL_MASK = "(내부 관리 — 관리자만 조회 가능)"


def _serialize_project(project: Project, key: ApiKey, org_ids: set[int]) -> ProjectOut:
    """git_url(리포 위치)은 관리자, 또는 그 프로젝트 조직 소속(전역 프로젝트는 누구나)
    사용자에게만 노출한다. 그 외에는 마스킹한다."""
    out = ProjectOut.model_validate(project)
    if project.organization:
        out.org_name = project.organization.name
    if not can_view_git_url(project, key, org_ids):
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
    org_ids = viewer_org_ids(db, key)
    return [_serialize_project(p, key, org_ids) for p in rows]


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
    return _serialize_project(project, key, viewer_org_ids(db, key))


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

    return _serialize_project(project, key, viewer_org_ids(db, key))


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """admin 권한으로 프로젝트를 삭제한다 — 플랫폼 등록 정보 한정.

    **Gitea 리포는 지우지 않는다.** 플랫폼에서 프로젝트를 내리는 것과 소스를 파기하는 것은
    되돌릴 수 있는 정도가 달라 분리한다(리포 정리는 Gitea에서 직접).
    삭제 대상은 프로젝트 레코드와 딸린 행(배포 이력·환경변수·모듈 바인딩·리다이렉트·프리뷰·
    기획/채팅 세션), 그리고 워크스페이스 클론이다. 감사 로그는 남긴다.
    """
    project = _get_project(db, project_id)
    name = project.name

    # 배포본 정지 — 레코드가 사라지면 콘솔에서 내릴 방법이 없어진다(런타임 미가용은 무시).
    if is_enabled("deploy"):
        units = ([f"{name}-{c}" for c in COMPOSITE_COMPONENTS]
                 if project.type == ProjectType.composite else [name])
        for unit in units:
            for profile in BuildProfile:
                try:
                    deployer.get_runtime().stop(unit, profile)
                except Exception:  # noqa: BLE001
                    pass

    session_ids = db.execute(
        select(ChatSession.id).where(ChatSession.project_id == project_id)
    ).scalars().all()
    if session_ids:
        for model in (ChatMessage, PlanArtifact):
            db.execute(sa_delete(model).where(model.session_id.in_(session_ids)))
    for model in (ChatSession, Deployment, EnvVar, ModuleBinding, RedirectRule,
                  PreviewSession, PortAllocation):
        db.execute(sa_delete(model).where(model.project_id == project_id))
    db.delete(project)
    db.commit()

    shutil.rmtree(get_settings().work_dir / name, ignore_errors=True)
    audit.record(db, admin.name, "project.delete", name, {"project_id": project_id})


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
    # release와 development는 같은 도메인에서 경로가 겹친다 — 동시에 띄우지 못하게 막는다.
    try:
        deployer.assert_no_profile_conflict(project, profile)
    except ProfileConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
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


@router.get("/{project_id}/deployments/{deployment_id}/build-log",
            dependencies=[Depends(require_feature("deploy"))])
def deployment_build_log(
    project_id: int,
    deployment_id: int,
    tail: int = 200,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """이 배포 레코드의 빌드/설치 로그 tail. build_log_path는 실제 빌드를 시작하기
    전에 미리 레코드에 커밋되므로(deployer.py 참고), 배포가 아직 진행 중(building)
    이거나 멈춰 있을 때도 그 시점까지의 로그와 "지금 실행 중인 명령"을 볼 수 있다."""
    _get_project(db, project_id)
    record = db.get(Deployment, deployment_id)
    if record is None or record.project_id != project_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    done = record.status != DeploymentStatus.building
    if not record.build_log_path:
        return {"content": "", "done": done}
    path = Path(record.build_log_path)
    if not path.is_file():
        return {"content": "", "done": done}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"content": "\n".join(lines[-tail:]), "done": done}


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


@router.get("/{project_id}/module-report", response_model=ProjectModuleReportOut)
def get_project_module_report(
    project_id: int,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """프로젝트 모듈 사용 이력 리포트 — 현재 바인딩된 모듈, 주입된 환경변수, 관련 작업 로그를 집계한다."""
    project = db.execute(
        select(Project).options(joinedload(Project.organization)).where(Project.id == project_id)
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    org_name = project.organization.name if project.organization else None

    # 1. 바인딩된 모듈 조회
    bindings = db.execute(
        select(ModuleBinding, Module)
        .join(Module, ModuleBinding.module_id == Module.id)
        .where(ModuleBinding.project_id == project_id)
    ).all()

    active_modules: list[ModuleUsageItem] = []
    total_injected_envs = 0

    from ..services.modules import _injected_keys_for  # noqa: PLC0415

    for binding, module in bindings:
        keys = _injected_keys_for(module.type.value, binding.env_prefix)
        total_injected_envs += len(keys)
        active_modules.append(
            ModuleUsageItem(
                id=module.id,
                name=module.name,
                type=module.type.value,
                category=module.category,
                env_prefix=binding.env_prefix,
                injected_env_keys=keys,
            )
        )

    # 2. 모듈 관련 감사 이벤트 (Audit History) 조회
    audit_rows = db.execute(
        select(AuditEvent)
        .where(
            (AuditEvent.target == project.name) | (AuditEvent.target == str(project_id))
        )
        .order_by(AuditEvent.created_at.desc())
        .limit(100)
    ).scalars()

    history: list[ModuleHistoryItem] = []
    for r in audit_rows:
        if r.action and (r.action.startswith("module.") or "module" in r.action):
            history.append(
                ModuleHistoryItem(
                    id=r.id,
                    actor=r.actor,
                    action=r.action,
                    target=r.target,
                    # 감사 표의 컬럼 이름은 detail이다(models.AuditEvent).
                    payload=r.detail or {},
                    created_at=r.created_at,
                )
            )

    return ProjectModuleReportOut(
        project_id=project.id,
        project_name=project.name,
        org_name=org_name,
        total_active_modules=len(active_modules),
        total_injected_envs=total_injected_envs,
        active_modules=active_modules,
        history=history,
    )
