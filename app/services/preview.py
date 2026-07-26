"""PreviewSession — 편집 브랜치의 TTL 임시 프리뷰.

development 프로필로 빌드하되 유닛 이름을 {project}-pv{id}로 분리해
정식 dev/release 배포와 독립적으로 기동·회수한다. 안전장치:
리소스는 dev 프로필의 절반, TTL 기본 60분, 만료 시 접근 시점에 자동 회수(lazy cleanup).
"""
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import BuildProfile, PreviewSession, PreviewStatus, Project
from . import proxy
from .build import build_image, checkout, internal_port
from .deployer import get_runtime, resolve_env
from .runtime.base import RuntimeSpec

DEFAULT_TTL_MINUTES = 60
MAX_ACTIVE_PREVIEWS = 5

_create_lock = threading.Lock()


def preview_unit_name(project_name: str, preview_id: int) -> str:
    return f"{project_name}-pv{preview_id}"


def preview_domain(project_name: str, preview_id: int) -> str:
    return f"{preview_unit_name(project_name, preview_id)}.{get_settings().base_domain}"


def is_expired(ps: PreviewSession, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    expires = ps.expires_at
    if expires.tzinfo is None:  # SQLite는 tz를 보존하지 않음
        expires = expires.replace(tzinfo=timezone.utc)
    return now >= expires


def create_preview_sync(
    db: Session, project: Project, branch: str, ttl_minutes: int = DEFAULT_TTL_MINUTES
) -> PreviewSession:
    with _create_lock:
        cleanup_expired(db, project)
        active = db.execute(
            select(PreviewSession).where(
                PreviewSession.project_id == project.id,
                PreviewSession.status == PreviewStatus.running,
            )
        ).scalars().all()
        if len(active) >= MAX_ACTIVE_PREVIEWS:
            raise TooManyPreviews(project.name)

        record = PreviewSession(
            project_id=project.id,
            branch=branch,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
        )
        db.add(record)
        db.commit()

    try:
        workdir, sha = checkout(project)
        if branch != project.branch:
            import subprocess  # noqa: PLC0415

            subprocess.run(["git", "checkout", branch], cwd=workdir, check=True,
                           capture_output=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workdir, check=True,
                                 capture_output=True, text=True).stdout.strip()
        result = build_image(project, workdir, sha, BuildProfile.development)

        unit = preview_unit_name(project.name, record.id)
        domain = preview_domain(project.name, record.id)
        settings = get_settings()
        spec = RuntimeSpec(
            project_name=unit,
            image_tag=result.image_tag,
            internal_port=internal_port(project.type, BuildProfile.development),
            profile=BuildProfile.development,
            domain=domain,
            env=resolve_env(db, project, BuildProfile.development),
            # 프리뷰는 dev 프로필에서 다시 절반 (LLM 생성 코드 실행 지점 — 상한 보수적으로)
            memory_limit=project.memory_limit or settings.default_memory_limit,
            cpu_limit=(project.cpu_limit or settings.default_cpu_limit) * 0.5,
            replicas=1,
            gpu=False,  # 프리뷰에는 GPU를 주지 않는다
            health_check_path=project.health_check_path,
        )
        endpoint = get_runtime().start(spec)
        if settings.tier == "small":
            proxy.configure(unit, BuildProfile.development, domain, "/", endpoint)
        record.url = f"https://{domain}"
        record.status = PreviewStatus.running
        db.commit()
        return record
    except Exception:
        record.status = PreviewStatus.failed
        db.commit()
        raise


def teardown(db: Session, ps: PreviewSession, project: Project) -> None:
    unit = preview_unit_name(project.name, ps.id)
    try:
        get_runtime().stop(unit, BuildProfile.development)
    except Exception:
        pass
    if get_settings().tier == "small":
        proxy.remove(unit, BuildProfile.development)
    ps.status = PreviewStatus.expired
    db.commit()


def cleanup_expired(db: Session, project: Project) -> int:
    rows = db.execute(
        select(PreviewSession).where(
            PreviewSession.project_id == project.id,
            PreviewSession.status == PreviewStatus.running,
        )
    ).scalars().all()
    count = 0
    for ps in rows:
        if is_expired(ps):
            teardown(db, ps, project)
            count += 1
    return count


class TooManyPreviews(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"active preview limit ({MAX_ACTIVE_PREVIEWS}) reached for {name}")
