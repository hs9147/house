"""백엔드 기동 시 자기 자신의 콘솔을 배포한다 (옵트인, PAAS_SELF_DEPLOY_CONSOLE).

콘솔은 monorepo(platform/console/) 서브폴더에 있으므로, Project.source_subdir로
빌드 컨텍스트를 지정한 일반 react 프로젝트로 등록해 기존 배포 파이프라인
(build_image → DockerRuntime → 리버스프록시)을 그대로 재사용한다 — 콘솔 전용 배포
경로를 새로 만들지 않는다.

최초 1회만 배포를 트리거하고(이미 배포 이력이 있으면 건너뜀), 이후 재기동마다
다시 빌드하지 않는다 — 갱신은 기존 프로젝트와 동일하게 /deploy 호출이나 웹훅으로.
"""
import logging

from sqlalchemy import select

from .. import git_policy
from ..config import get_settings
from ..db import SessionLocal
from ..models import BuildProfile, Deployment, Organization, Project, ProjectType
from . import deployer

logger = logging.getLogger(__name__)

SELF_CONSOLE_PROJECT_NAME = "paas-console"
SELF_CONSOLE_SUBDIR = "platform/console"
# 콘솔 자기 배포는 admin 조직 소속으로 등록한다 — /apps/admin/paas-console/ 경로가 된다.
# Gitea 조직 자동 생성(services/gitea.py)은 거치지 않는다 — git_url이 이미 주어져
# 있어(이 리포 자신) 새 리포를 만들 필요가 없기 때문.
SELF_DEPLOY_ORG_NAME = "admin"


def bootstrap_console_deploy() -> None:
    settings = get_settings()
    if not settings.self_deploy_console:
        return
    if not settings.self_deploy_console_git_url:
        logger.warning(
            "PAAS_SELF_DEPLOY_CONSOLE=true지만 PAAS_SELF_DEPLOY_CONSOLE_GIT_URL이 "
            "비어 있어 콘솔 자기 배포를 건너뜁니다."
        )
        return
    try:
        git_policy.enforce_internal_git_url(settings.self_deploy_console_git_url)
    except Exception as e:  # noqa: BLE001 — 부트스트랩 실패로 앱 기동 자체를 막지 않음
        logger.warning("콘솔 자기 배포를 건너뜁니다 (git_url 정책 위반): %s", e)
        return

    with SessionLocal() as db:
        project = db.execute(
            select(Project).where(Project.name == SELF_CONSOLE_PROJECT_NAME)
        ).scalar_one_or_none()
        if project is None:
            org = db.execute(
                select(Organization).where(Organization.name == SELF_DEPLOY_ORG_NAME)
            ).scalar_one_or_none()
            if org is None:
                org = Organization(name=SELF_DEPLOY_ORG_NAME)
                db.add(org)
                db.commit()
                db.refresh(org)

            project = Project(
                name=SELF_CONSOLE_PROJECT_NAME,
                type=ProjectType.react,
                organization_id=org.id,
                git_url=settings.self_deploy_console_git_url,
                branch=settings.self_deploy_console_branch,
                source_subdir=SELF_CONSOLE_SUBDIR,
                health_check_path="/",
            )
            db.add(project)
            db.commit()
            db.refresh(project)

        already_deployed = db.execute(
            select(Deployment.id).where(Deployment.project_id == project.id).limit(1)
        ).first()
        if already_deployed:
            return

        try:
            deployer.deploy_queued(db, project, BuildProfile.release)
        except Exception:  # noqa: BLE001 — 부트스트랩 실패로 앱 기동 자체를 막지 않음
            logger.exception("콘솔 자기 배포 트리거 실패")
