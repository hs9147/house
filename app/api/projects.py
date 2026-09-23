import asyncio
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
    LlmProvider,
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
    ProjectSourceSubdirSet,
    StartScriptOut,
    StartScriptProposeIn,
    StartScriptSet,
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
from ..services import build as build_module
from ..services import (
    deploydiag, deployer, gitea, startscript, structure, upload, workspace,
)
from ..services.build import COMPOSITE_COMPONENTS, checkout
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


def _composite_units(project: Project) -> list[str]:
    """복합 프로젝트의 배포 유닛 이름 — **감지된 구조**가 원천이다.

    예전에는 ("backend", "frontend") 고정 목록을 썼다. 복합 배포는 감지된 구조로
    일반화됐는데(services/structure) 여기만 남아서, `api/`+`web/`처럼 이름이 다른 리포는
    중지·상태 조회가 없는 유닛을 가리켰다 — 중지가 아무것도 내리지 않고 상태는 늘
    stopped였다. 구조가 없는 예전 레코드는 그때의 규칙으로 떨어진다.
    """
    names = [str(c.get("name")) for c in (project.structure or {}).get("components") or []]
    return [n for n in names if n] or list(COMPOSITE_COMPONENTS)


def _composite_status(runtime, project: Project, profile: BuildProfile) -> str:
    """복합 프로젝트의 프로필 상태를 **한 문자열로** 집계한다.

    예전에는 컴포넌트별 dict를 돌려줬는데, 화면은 프로필당 문자열을 기대한다 —
    StatusPill이 객체에 .split()을 불러 프로젝트 조회 진입 자체가 실패했다(실측).
    응답 모양이 타입에 따라 갈리면 화면은 언젠가 한쪽을 잊는다.

    "progressing (1/2)" 형태는 StatusPill이 이미 지원하는 표기다(첫 낱말로 색을 정한다).
    """
    units = _composite_units(project)
    values = [runtime.status(f"{project.name}-{unit}", profile) for unit in units]
    total = len(values)
    running = sum(v == "running" for v in values)
    if running == total:
        return "running" if total == 1 else f"running ({running}/{total})"
    if running:
        return f"progressing ({running}/{total})"
    # 아무것도 안 돌 때 — 실패가 섞여 있으면 그것을 말한다(stopped로 뭉개면 원인이 숨는다).
    if "failed" in values:
        return f"failed (0/{total})"
    if len(set(values)) == 1:
        return values[0]
    return f"stopped (0/{total})"


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
        health_check_path=health_check_path,
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
        health_check_path=form.health_check_path,
        default_profile=form.default_profile,
    )
    db.add(project)
    db.commit()
    audit.record(db, key.name, "project.upload", project.name, {"git_sha": git_sha})

    # 올린 소스에서 배포 단위를 읽어 둔다 — 사용자가 고른 type 하나로는 백엔드+프론트엔드
    # 같은 구성을 표현할 수 없고, 배포 시점에 폴더 이름으로 다시 추측할 수도 없다.
    # 감지된 구조가 사용자가 고른 type과 다르면 대표 타입도 감지값으로 맞춘다
    # (services/structure.refresh) — 올린 소스가 사실이다.
    structure.refresh(db, project, workdir, actor=key.name, source="repo", git_sha=git_sha)

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
        units = ([f"{name}-{c}" for c in _composite_units(project)]
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


@router.post("/{project_id}/structure/detect", response_model=ProjectOut)
async def detect_project_structure(
    project_id: int,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """리포 구조를 지금 다시 읽어 저장한다 — **배포 전에 확인하는 유일한 길**이다.

    배포 시점에도 자동으로 다시 읽지만(services/deployer), 그때는 이미 빌드가 시작된
    뒤다. 구조가 바뀌었는지 먼저 보고 싶을 때 여기서 확인한다.

    리포를 최신화한 뒤 감지한다 — 체크아웃이 실패하면 있는 워킹카피로 판정하고, 감지된
    컴포넌트가 하나도 없으면 저장된 구조를 지우지 않는다(services/structure.refresh).
    """
    project = _get_project(db, project_id)
    workdir = get_settings().work_dir / project.name
    git_sha = ""
    try:
        workdir, git_sha = await asyncio.to_thread(checkout, project)
    except Exception:  # noqa: BLE001 — 원격이 없어도 워킹카피가 있으면 읽는다
        pass
    await asyncio.to_thread(
        structure.refresh, db, project, workdir,
        actor=key.name, source="repo", git_sha=git_sha,
    )
    return _serialize_project(project, key, viewer_org_ids(db, key))


def _script_components(project: Project) -> list[str]:
    """스크립트를 따로 둘 수 있는 유닛 목록. 단일 배포는 비어 있다("" 하나뿐).

    복합 배포는 컴포넌트마다 유닛·포트·공개 경로가 따로이므로 스크립트도 따로여야 한다 —
    한 스크립트가 둘을 띄우면 서비스 감시자(nssm)는 자식 하나만 보고, 헬스체크도 포트
    하나만 본다. 목록은 감지된 구조에서 온다(_composite_units와 같은 원천).
    """
    return _composite_units(project) if project.type == ProjectType.composite else []


def _validate_component(project: Project, component: str) -> str:
    """요청이 가리킨 유닛을 확인한다. 없는 컴포넌트는 조용히 ""로 떨어지면 안 된다 —
    그러면 컴포넌트 스크립트를 저장한 줄 알고 단일 스크립트를 덮어쓴다."""
    if not component:
        return ""
    if component not in _script_components(project):
        raise HTTPException(
            status_code=404,
            detail=f"감지된 구조에 없는 컴포넌트입니다: {component}",
        )
    return component


@router.get("/{project_id}/start-script", response_model=StartScriptOut,
            dependencies=[Depends(require_feature("deploy"))])
def get_start_script(
    project_id: int,
    component: str = "",
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """지금 쓰이는 기동 스크립트 — 이 유닛에 지정된 것이 있으면 그것, 없으면 템플릿."""
    project = _get_project(db, project_id)
    component = _validate_component(project, component)
    saved = (project.start_scripts or {}).get(component)
    common = {"component": component, "components": _script_components(project)}
    if saved:
        return StartScriptOut(script=saved, problems=startscript.validate(saved),
                              source="project", **common)
    return StartScriptOut(script=build_module._START_SCRIPT, source="template", **common)


@router.post("/{project_id}/start-script/propose", response_model=StartScriptOut,
             dependencies=[Depends(require_feature("deploy"))])
async def propose_start_script(
    project_id: int,
    body: StartScriptProposeIn,
    component: str = "",
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """LLM이 리포를 보고 기동 스크립트를 쓴다 — **저장하지 않는다.**

    이 스크립트는 서버에서 서비스 권한으로 실행된다. LLM이 쓴 것을 그대로 저장·실행하면
    그것은 원격 코드 실행이다. 그래서 검증 결과를 함께 돌려주고, 저장은 사람이 확인한 뒤
    별도 요청으로 한다(PUT). 프로바이더도 사람이 고른다 — 어느 모델로 쓸지 추측하지 않는다.
    """
    project = _get_project(db, project_id)
    component = _validate_component(project, component)
    provider = db.get(LlmProvider, body.provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="LLM provider not found")
    try:
        workdir = await asyncio.to_thread(workspace.code_workdir, project)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"리포를 가져올 수 없습니다: {str(e)[:300]}")
    try:
        result = await asyncio.to_thread(
            startscript.propose, db, project, provider, workdir, component)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"스크립트 작성 실패: {str(e)[:500]}")
    audit.record(db, admin.name, "project.start_script.propose", project.name,
                 {"provider": provider.name, "component": component,
                  "problems": len(result["problems"])})
    return StartScriptOut(source="project", component=component,
                          components=_script_components(project), **result)


@router.put("/{project_id}/start-script", response_model=StartScriptOut,
            dependencies=[Depends(require_feature("deploy"))])
def set_start_script(
    project_id: int,
    body: StartScriptSet,
    component: str = "",
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """확인한 스크립트를 저장한다 — 검증을 통과하지 못하면 거부한다.

    경고만 하고 통과시키면 아무도 읽지 않는다. 다음 배포에서 이 스크립트가 해당 유닛의
    폴더에 start.cmd로 쓰인다.
    """
    project = _get_project(db, project_id)
    component = _validate_component(project, component)
    problems = startscript.validate(body.script)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))
    # JSON 컬럼은 **새 dict를 대입**해야 변경으로 잡힌다 — 제자리에서 고치면 SQLAlchemy가
    # 더티로 보지 않아 조용히 저장되지 않는다.
    project.start_scripts = {**(project.start_scripts or {}), component: body.script}
    db.commit()
    audit.record(db, admin.name, "project.start_script.set", project.name,
                 {"component": component, "chars": len(body.script)})
    return StartScriptOut(script=body.script, source="project", component=component,
                          components=_script_components(project))


@router.delete("/{project_id}/start-script", response_model=StartScriptOut,
               dependencies=[Depends(require_feature("deploy"))])
def reset_start_script(
    project_id: int,
    component: str = "",
    db: Session = Depends(get_db),
    admin: ApiKey = Depends(require_admin),
):
    """이 유닛의 지정을 지우고 템플릿으로 되돌린다 — 되돌릴 길이 없으면 아무도 지정하지 않는다."""
    project = _get_project(db, project_id)
    component = _validate_component(project, component)
    remaining = {k: v for k, v in (project.start_scripts or {}).items() if k != component}
    project.start_scripts = remaining or None
    db.commit()
    audit.record(db, admin.name, "project.start_script.reset", project.name,
                 {"component": component})
    return StartScriptOut(script=build_module._START_SCRIPT, source="template",
                          component=component, components=_script_components(project))


@router.put("/{project_id}/source-subdir", response_model=ProjectOut)
def set_source_subdir(
    project_id: int,
    body: ProjectSourceSubdirSet,
    db: Session = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
):
    """빌드 대상 폴더를 바꾼다 — 배포 실패 진단의 제안을 적용하는 자리.

    다음 배포에서 배포 스크립트(start.cmd)와 빌드 컨텍스트가 이 폴더 기준으로 다시 만들어진다
    (services/build). 리포에 실제로 있는 폴더인지 여기서 확인한다 — 없는 폴더를 넣으면
    다음 배포가 같은 자리에서 다시 실패하고, 그때는 원인이 하나 더 늘어난다.
    """
    project = _get_project(db, project_id)
    value = body.source_subdir.strip().strip("/")
    if value:
        workdir = get_settings().work_dir / project.name
        if workdir.exists() and not (workdir / value).is_dir():
            raise HTTPException(
                status_code=422,
                detail=f"리포에 그런 폴더가 없습니다: {value} (워킹카피 기준)",
            )
    project.source_subdir = value or None
    db.commit()
    audit.record(db, key.name, "project.source_subdir", project.name, {"value": value})
    return _serialize_project(project, key, viewer_org_ids(db, key))


@router.get("/{project_id}/deploy/diagnose",
            dependencies=[Depends(require_feature("deploy"))])
async def diagnose_deploy_failure(
    project_id: int,
    profile: BuildProfile = BuildProfile.release,
    db: Session = Depends(get_db),
    _: ApiKey = Depends(require_api_key),
):
    """마지막 실패한 배포를 진단한다 — 원인과 **고칠 것**을 돌려준다(바꾸지는 않는다).

    적용과 재시도를 여기서 하지 않는 이유: 배포는 되돌리기 쉬운 일이 아니고, 고침이
    프로젝트 설정(source_subdir)을 바꾸는 경우도 있다. 사람이 진단을 보고 확인하거나
    취소해야 한다(콘솔이 그 자리에서 묻는다).
    """
    project = _get_project(db, project_id)
    last = db.execute(
        select(Deployment)
        .where(Deployment.project_id == project_id, Deployment.profile == profile)
        .order_by(Deployment.id.desc())
    ).scalars().first()
    if last is None:
        raise HTTPException(status_code=404, detail="이 프로필의 배포 이력이 없습니다.")

    # 리포를 최신으로 두고 본다 — 실패 원인이 이미 고쳐졌을 수도 있다(그러면 그렇게 말해야 한다).
    try:
        workdir, _sha = await asyncio.to_thread(checkout, project)
    except Exception:  # noqa: BLE001 — 원격이 없어도 워킹카피로 진단한다
        workdir = get_settings().work_dir / project.name

    detected = structure.detect(workdir) if workdir.exists() else {"components": []}
    result = deploydiag.diagnose(
        workdir,
        source_subdir=project.source_subdir or "",
        is_streamlit=project.type == ProjectType.streamlit,
        error=last.error or "",
        log_path=last.build_log_path,
    )
    return {
        "deployment_id": last.id,
        "status": last.status.value,
        "profile": profile.value,
        "source_subdir": project.source_subdir or "",
        "detected": structure.summary(detected) or "(감지된 배포 단위 없음)",
        **result,
    }


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
        for name in _composite_units(project):
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
            for name in _composite_units(project)
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
        return {p.value: _composite_status(runtime, project, p) for p in BuildProfile}
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
