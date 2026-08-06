"""배포 오케스트레이터.

checkout → build(프로필별) → runtime 기동(1차 Docker / 2차 K8s) → (1차만) Caddy 전환.
프로젝트별 락으로 동시 배포를 1건으로 제한한다 (웹훅 연속 push 대비).
"""
import asyncio
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import BuildProfile, Deployment, DeploymentStatus, EnvVar, Project, RedirectRule
from ..security import decrypt_value
from . import proxy
from .build import (
    COMPOSITE_COMPONENTS,
    PROFILES,
    BuildError,
    build_image,
    checkout,
    detect_composite_components,
    docker_build_log_path,
    env_setup_log_path,
    install_dependencies,
    internal_port,
    write_start_script,
)
from .runtime.base import Endpoint, Runtime, RuntimeSpec

_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


def get_runtime() -> Runtime:
    settings = get_settings()
    if settings.tier == "enterprise":
        from .runtime.k8s_runtime import K8sRuntime  # noqa: PLC0415

        return K8sRuntime()
    if settings.runtime_backend == "windows_service":
        from .runtime.windows_service_runtime import WindowsServiceRuntime  # noqa: PLC0415

        return WindowsServiceRuntime()
    from .runtime.docker_runtime import DockerRuntime  # noqa: PLC0415

    return DockerRuntime()


def _org_name(project: Project) -> str | None:
    return project.organization.name if project.organization else None


def redirects_for(db: Session, project: Project) -> list[RedirectRule]:
    return list(
        db.execute(
            select(RedirectRule).where(RedirectRule.project_id == project.id)
        ).scalars()
    )


def resolve_env(db: Session, project: Project, profile: BuildProfile) -> dict[str, str]:
    from . import modules  # noqa: PLC0415 — 순환 import 회피

    env = dict(PROFILES[profile].env)
    env.update(modules.env_for_project(db, project))  # 바인딩된 Module 자동 주입
    rows = db.execute(select(EnvVar).where(EnvVar.project_id == project.id)).scalars()
    for row in rows:
        env[row.key] = decrypt_value(row.value_encrypted)  # 프로젝트 EnvVar가 최우선
    return env


def secret_env_keys(db: Session, project: Project) -> frozenset[str]:
    """민감 env 키 이름 집합 — K8s 매니페스트에서 Secret으로 분리하는 기준.

    EnvVar.is_secret=True 행 + module 바인딩이 주입하는 *_API_KEY/*_DSN
    (modules.SENSITIVE_KEYS 규약, `binding_env`가 생성하는 이름과 동일).
    """
    rows = db.execute(
        select(EnvVar.key).where(EnvVar.project_id == project.id, EnvVar.is_secret.is_(True))
    ).scalars()
    keys = set(rows)
    from . import modules  # noqa: PLC0415 — 순환 import 회피

    keys.update(
        k for k in modules.env_for_project(db, project) if k.endswith(("_API_KEY", "_DSN"))
    )
    return frozenset(keys)


def make_spec(
    db: Session, project: Project, image_tag: str, profile: BuildProfile,
    *, component: str | None = None, internal_port_override: int | None = None,
) -> RuntimeSpec:
    """component가 주어지면(composite 전용) unit_name·이미지가 컴포넌트별로 분리되고,
    internal_port_override로 해당 컴포넌트의 실제 내부 포트를 지정한다(빌드 시점에
    감지된 타입 기준 — project.type은 composite 자체라 포트 매핑이 없다)."""
    from .host import gpu_allowed  # noqa: PLC0415

    settings = get_settings()
    port = internal_port_override if internal_port_override is not None else internal_port(project.type, profile)
    return RuntimeSpec(
        project_name=project.name,
        image_tag=image_tag,
        internal_port=port,
        profile=profile,
        domain=proxy.domain_for(project.name, project.domain, profile),
        env=resolve_env(db, project, profile),
        secret_keys=secret_env_keys(db, project),
        memory_limit=project.memory_limit or settings.default_memory_limit,
        cpu_limit=project.cpu_limit or settings.default_cpu_limit,
        replicas=PROFILES[profile].replicas,
        gpu=project.type.value == "llm" and gpu_allowed(),
        health_check_path=project.health_check_path,
        component=component,
    )


def deploy_sync(
    db: Session, project: Project, profile: BuildProfile, git_sha: str | None = None,
    record: Deployment | None = None,
) -> Deployment:
    """블로킹 배포 파이프라인. API에서는 스레드로 위임해 이벤트 루프를 막지 않는다.

    record가 주어지면(큐 경로에서 선생성) 새로 만들지 않고 그 레코드를 채운다.
    """
    lock = _locks[project.id]
    if not lock.acquire(blocking=False):
        if record is not None:
            record.status = DeploymentStatus.failed
            record.error = f"deployment already in progress for {project.name}"
            record.finished_at = datetime.now(timezone.utc)
            db.commit()
        raise DeployInProgress(project.name)
    try:
        workdir, sha = checkout(project, git_sha)
        if record is None:
            record = Deployment(
                project_id=project.id,
                git_sha=sha,
                image_tag="",
                profile=profile,
                status=DeploymentStatus.building,
            )
            db.add(record)
        else:
            record.git_sha = sha
        db.commit()
        try:
            if get_settings().runtime_backend == "windows_service":
                # windows_service 런타임은 이미지 대신 리포 루트의 start.cmd를 nssm으로
                # Windows Service에 등록해 네이티브 실행한다 — docker build를 건너뛴다
                # (image_tag는 이 런타임이 사용하지 않는다). start.cmd를 조건 없이
                # 자동 생성한다(dockerfile_for와 대칭 — 매 배포 시 갱신).
                write_start_script(workdir)
                # npm/pip install을 여기서 먼저 끝낸다(build_image의 docker build와 대응
                # 되는 명시적 build 단계) — runtime.start()의 헬스체크 창 안에서 설치까지
                # 겸하면, 설치가 느릴 때 원인이 "헬스체크 실패"로만 보이고 배포 상태도
                # 실패로 남지 않는다(install_dependencies 참고).
                # build_log_path는 install_dependencies를 부르기 전에 커밋한다 — 진행
                # 중(아직 building 상태)에도 GET .../build-log로 지금까지의 로그를 볼 수
                # 있어야 한다(끝난 뒤에야 경로를 알면 그 전엔 조회할 방법이 없다).
                log_path = env_setup_log_path(project.name, sha, profile)
                record.build_log_path = str(log_path)
                db.commit()
                install_dependencies(workdir, log_path)
                image_tag = ""
            else:
                log_path = docker_build_log_path(project.name, sha, profile)
                record.build_log_path = str(log_path)
                db.commit()
                result = build_image(project, workdir, sha, profile)
                record.image_tag = result.image_tag
                image_tag = result.image_tag

            spec = make_spec(db, project, image_tag, profile)
            endpoint = get_runtime().start(spec)
            if get_settings().tier == "small":
                path_prefix = proxy.path_prefix_for(_org_name(project), project.name, project.domain, profile)
                proxy.configure(
                    project.name, profile, spec.domain, path_prefix, endpoint,
                    redirects_for(db, project),
                )
                record.host_port = endpoint.port

            record.status = DeploymentStatus.running
            record.finished_at = datetime.now(timezone.utc)
            db.commit()
            _mark_previous_stopped(db, record)
            return record
        except (BuildError, RuntimeError) as e:
            record.status = DeploymentStatus.failed
            record.error = str(e)
            if isinstance(e, BuildError) and e.log_path:
                record.build_log_path = str(e.log_path)
            record.finished_at = datetime.now(timezone.utc)
            db.commit()
            raise
    finally:
        lock.release()


async def deploy(
    db: Session, project: Project, profile: BuildProfile, git_sha: str | None = None
) -> Deployment:
    return await asyncio.to_thread(deploy_sync, db, project, profile, git_sha)


def deploy_queued(
    db: Session, project: Project, profile: BuildProfile, git_sha: str | None = None
) -> Deployment:
    """비동기 배포(갭2): building 레코드를 즉시 만들고 파이프라인은 작업 큐에서 실행.

    반환된 레코드 id로 GET /projects/{id}/deployments 폴링으로 진행을 확인한다.
    """
    from ..db import SessionLocal  # noqa: PLC0415
    from . import jobs  # noqa: PLC0415

    record = Deployment(
        project_id=project.id,
        git_sha=git_sha or "",
        image_tag="",
        profile=profile,
        status=DeploymentStatus.building,
    )
    db.add(record)
    db.commit()
    record_id, project_id = record.id, project.id

    def _task() -> None:
        with SessionLocal() as session:
            proj = session.get(Project, project_id)
            rec = session.get(Deployment, record_id)
            if proj is None or rec is None:
                return
            try:
                deploy_sync(session, proj, profile, git_sha, record=rec)
            except Exception:
                # 실패 상태·에러는 deploy_sync가 레코드에 기록함
                pass

    jobs.submit(_task)
    return record


async def deploy_composite(
    db: Session, project: Project, profile: BuildProfile, git_sha: str | None = None
) -> dict[str, Deployment]:
    return await asyncio.to_thread(deploy_composite_sync, db, project, profile, git_sha)


def deploy_composite_queued(
    db: Session, project: Project, profile: BuildProfile, git_sha: str | None = None
) -> dict[str, Deployment]:
    """composite 프로젝트의 비동기 배포 — deploy_queued와 동일한 패턴이나 backend/frontend
    두 행을 미리 만들어 즉시 반환한다(컨벤션상 컴포넌트명은 항상 이 둘)."""
    from ..db import SessionLocal  # noqa: PLC0415
    from . import jobs  # noqa: PLC0415

    records = {
        name: Deployment(
            project_id=project.id, git_sha=git_sha or "", image_tag="", profile=profile,
            status=DeploymentStatus.building, component=name,
        )
        for name in COMPOSITE_COMPONENTS
    }
    for rec in records.values():
        db.add(rec)
    db.commit()
    record_ids = {name: rec.id for name, rec in records.items()}
    project_id = project.id

    def _task() -> None:
        with SessionLocal() as session:
            proj = session.get(Project, project_id)
            recs = {name: session.get(Deployment, rid) for name, rid in record_ids.items()}
            if proj is None or any(r is None for r in recs.values()):
                return
            try:
                deploy_composite_sync(session, proj, profile, git_sha, records=recs)
            except Exception:
                # 실패 상태·에러는 deploy_composite_sync가 레코드에 기록함
                pass

    jobs.submit(_task)
    return records


def rollback(db: Session, project: Project, profile: BuildProfile) -> Deployment:
    """직전 성공 배포의 이미지로 재기동 — 재빌드 없음."""
    rows = (
        db.execute(
            select(Deployment)
            .where(
                Deployment.project_id == project.id,
                Deployment.profile == profile,
                Deployment.image_tag != "",
            )
            .order_by(Deployment.id.desc())
        )
        .scalars()
        .all()
    )
    current = next((d for d in rows if d.status == DeploymentStatus.running), None)
    candidates = [
        d
        for d in rows
        if d.status in (DeploymentStatus.stopped, DeploymentStatus.running)
        and (current is None or d.id < current.id)
    ]
    if not candidates:
        raise NoRollbackTarget(project.name)
    target = candidates[0]

    spec = make_spec(db, project, target.image_tag, profile)
    endpoint = get_runtime().start(spec)
    if get_settings().tier == "small":
        path_prefix = proxy.path_prefix_for(_org_name(project), project.name, project.domain, profile)
        proxy.configure(
            project.name, profile, spec.domain, path_prefix, endpoint, redirects_for(db, project),
        )

    record = Deployment(
        project_id=project.id,
        git_sha=target.git_sha,
        image_tag=target.image_tag,
        profile=profile,
        status=DeploymentStatus.running,
        host_port=endpoint.port if get_settings().tier == "small" else None,
        finished_at=datetime.now(timezone.utc),
    )
    db.add(record)
    db.commit()
    _mark_previous_stopped(db, record)
    return record


def _mark_previous_stopped(db: Session, new_record: Deployment) -> None:
    # component는 일반 프로젝트에서 항상 None이고 SQLAlchemy는 `== None`을 IS NULL로
    # 번역하므로, 이 필터를 추가해도 기존 단일 컴포넌트 프로젝트의 조회 결과는 그대로다.
    db.query(Deployment).filter(
        Deployment.project_id == new_record.project_id,
        Deployment.profile == new_record.profile,
        Deployment.component == new_record.component,
        Deployment.status == DeploymentStatus.running,
        Deployment.id != new_record.id,
    ).update({"status": DeploymentStatus.stopped})
    db.commit()


def deploy_composite_sync(
    db: Session, project: Project, profile: BuildProfile, git_sha: str | None = None,
    records: dict[str, Deployment] | None = None,
) -> dict[str, Deployment]:
    """composite 프로젝트(backend/frontend) 전용 배포 파이프라인.

    두 컴포넌트를 순서대로 빌드·기동한다. 어느 한쪽이 실패하면 실패한 컴포넌트만
    직전 정상 이미지로 되돌리고(재빌드 없음), 두 엔드포인트가 모두 확보된 뒤에만
    ``proxy.configure_paths``를 정확히 한 번 호출한다 — 그전까지는 이전 배포가 그대로
    트래픽을 받으므로 부분 실패가 서비스 중단으로 이어지지 않는다.
    """
    lock = _locks[project.id]
    if not lock.acquire(blocking=False):
        if records:
            for rec in records.values():
                rec.status = DeploymentStatus.failed
                rec.error = f"deployment already in progress for {project.name}"
                rec.finished_at = datetime.now(timezone.utc)
            db.commit()
        raise DeployInProgress(project.name)
    try:
        if get_settings().runtime_backend == "windows_service":
            # windows_service는 리포 루트의 단일 start.cmd만 실행하므로 backend/frontend
            # 두 컴포넌트를 네이티브로 나눠 띄울 수 없다 — docker build로 조용히 실패하지 않고
            # 여기서 명확히 실패시킨다.
            msg = (
                f"{project.name}: windows_service 런타임은 composite(backend/frontend) "
                "프로젝트를 지원하지 않습니다 — 리포 루트의 단일 start.cmd만 실행합니다. "
                "docker 런타임을 쓰거나 각 컴포넌트를 별도 프로젝트로 분리하세요."
            )
            if records:
                for rec in records.values():
                    rec.status = DeploymentStatus.failed
                    rec.error = msg
                    rec.finished_at = datetime.now(timezone.utc)
                db.commit()
            raise BuildError(msg)
        workdir, sha = checkout(project, git_sha)
        components = detect_composite_components(workdir)
        if not components:
            raise BuildError(
                f"{project.name}: composite 프로젝트인데 backend/, frontend/ 서브폴더가 없습니다"
            )

        group_id = uuid.uuid4().hex
        if records is None:
            records = {
                name: Deployment(
                    project_id=project.id, git_sha=sha, image_tag="", profile=profile,
                    status=DeploymentStatus.building, component=name, deploy_group_id=group_id,
                )
                for name in components
            }
            for rec in records.values():
                db.add(rec)
        else:
            for rec in records.values():
                rec.git_sha = sha
                rec.deploy_group_id = group_id
        db.commit()

        endpoints: dict[str, Endpoint] = {}
        failed_component: str | None = None
        failure: Exception | None = None
        for name, comp_type in components.items():
            rec = records[name]
            # 이 컴포넌트의 빌드를 시작하기 전에 로그 경로를 커밋한다 — build_image 호출이
            # 오래 걸리거나 멈춰도 그 시점까지의 진행 상황을 조회할 수 있어야 한다.
            rec.build_log_path = str(docker_build_log_path(project.name, sha, profile, name))
            db.commit()
            try:
                result = build_image(
                    project, workdir, sha, profile, component=name, component_type=comp_type,
                )
                rec.image_tag = result.image_tag
                rec.internal_port = result.internal_port
                db.commit()

                spec = make_spec(
                    db, project, result.image_tag, profile,
                    component=name, internal_port_override=result.internal_port,
                )
                endpoints[name] = get_runtime().start(spec)
            except (BuildError, RuntimeError) as e:
                failed_component = name
                failure = e
                rec.status = DeploymentStatus.failed
                rec.error = str(e)
                if isinstance(e, BuildError) and e.log_path:
                    rec.build_log_path = str(e.log_path)
                rec.finished_at = datetime.now(timezone.utc)
                db.commit()
                break

        restored_targets: dict[str, Deployment] = {}
        if failed_component:
            try:
                endpoint, target = _restore_component(db, project, profile, failed_component)
                endpoints[failed_component] = endpoint
                restored_targets[failed_component] = target
            except NoRollbackTarget:
                pass  # 이 컴포넌트의 첫 배포부터 실패 — 엔드포인트 없이 진행(전체 실패로 기록)

        if len(endpoints) == len(components):
            if get_settings().tier == "small":
                domain = proxy.domain_for(project.name, project.domain, profile)
                base_prefix = proxy.path_prefix_for(
                    _org_name(project), project.name, project.domain, profile,
                )
                routes = [
                    proxy.PathRoute(path_prefix=base_prefix + "api/", endpoint=endpoints["backend"]),
                    proxy.PathRoute(path_prefix=base_prefix, endpoint=endpoints["frontend"]),
                ]
                proxy.configure_paths(
                    project.name, profile, domain, routes, redirects_for(db, project),
                )
            for name in components:
                if name == failed_component:
                    continue
                rec = records[name]
                rec.host_port = endpoints[name].port
                rec.status = DeploymentStatus.running
                rec.finished_at = datetime.now(timezone.utc)
                _mark_previous_stopped(db, rec)
            for target in restored_targets.values():
                _mark_previous_stopped(db, target)
            db.commit()
        elif failed_component:
            # 복구 불가(되돌릴 이전 버전이 없음) — 프록시는 절대 건드리지 않으므로
            # 서비스는 이전 상태 그대로다. 성공했던 컴포넌트도 이번 시도 전체를
            # 실패로 기록해 building 상태로 남지 않게 한다(컨테이너 자체는 다음
            # 배포 시도의 blue/green 교체로 자연스럽게 정리된다).
            for name in components:
                rec = records[name]
                if rec.status == DeploymentStatus.building:
                    rec.status = DeploymentStatus.failed
                    rec.error = (
                        f"{failed_component} 컴포넌트에 되돌릴 이전 버전이 없어 "
                        "배포 전체를 취소했습니다"
                    )
                    rec.finished_at = datetime.now(timezone.utc)
            db.commit()

        if failed_component:
            raise failure
        return records
    finally:
        lock.release()


def _restore_component(
    db: Session, project: Project, profile: BuildProfile, component: str,
) -> tuple[Endpoint, Deployment]:
    """실패한 컴포넌트를 직전 정상 이미지로 재빌드 없이 재기동한다(단일 컴포넌트
    rollback()과 동일한 탐색 규칙을 component 단위로 적용)."""
    rows = (
        db.execute(
            select(Deployment)
            .where(
                Deployment.project_id == project.id,
                Deployment.profile == profile,
                Deployment.component == component,
                Deployment.image_tag != "",
            )
            .order_by(Deployment.id.desc())
        )
        .scalars()
        .all()
    )
    candidates = [
        d for d in rows if d.status in (DeploymentStatus.stopped, DeploymentStatus.running)
    ]
    if not candidates:
        raise NoRollbackTarget(f"{project.name}:{component}")
    target = candidates[0]
    spec = make_spec(
        db, project, target.image_tag, profile,
        component=component, internal_port_override=target.internal_port,
    )
    endpoint = get_runtime().start(spec)
    target.status = DeploymentStatus.running
    return endpoint, target


def rollback_composite(db: Session, project: Project, profile: BuildProfile) -> dict[str, Deployment]:
    """현재 배포 그룹 이전, backend/frontend 이미지가 모두 갖춰진 가장 최근
    deploy_group_id로 되돌린다 — 재빌드 없음."""
    rows = (
        db.execute(
            select(Deployment)
            .where(
                Deployment.project_id == project.id,
                Deployment.profile == profile,
                Deployment.deploy_group_id.is_not(None),
                Deployment.image_tag != "",
            )
            .order_by(Deployment.id.desc())
        )
        .scalars()
        .all()
    )
    groups: dict[str, dict[str, Deployment]] = {}
    order: list[str] = []
    for row in rows:
        if row.deploy_group_id not in groups:
            groups[row.deploy_group_id] = {}
            order.append(row.deploy_group_id)
        groups[row.deploy_group_id].setdefault(row.component, row)

    complete = [gid for gid in order if len(groups[gid]) == 2]
    current = next(
        (gid for gid in complete
         if all(d.status == DeploymentStatus.running for d in groups[gid].values())),
        None,
    )
    candidates = [gid for gid in complete if gid != current]
    if not candidates:
        raise NoRollbackTarget(project.name)
    target_group = groups[candidates[0]]

    endpoints: dict[str, Endpoint] = {}
    for name, target in target_group.items():
        spec = make_spec(
            db, project, target.image_tag, profile,
            component=name, internal_port_override=target.internal_port,
        )
        endpoints[name] = get_runtime().start(spec)

    if get_settings().tier == "small":
        domain = proxy.domain_for(project.name, project.domain, profile)
        base_prefix = proxy.path_prefix_for(_org_name(project), project.name, project.domain, profile)
        routes = [
            proxy.PathRoute(path_prefix=base_prefix + "api/", endpoint=endpoints["backend"]),
            proxy.PathRoute(path_prefix=base_prefix, endpoint=endpoints["frontend"]),
        ]
        proxy.configure_paths(project.name, profile, domain, routes, redirects_for(db, project))

    group_id = uuid.uuid4().hex
    records: dict[str, Deployment] = {}
    for name, target in target_group.items():
        rec = Deployment(
            project_id=project.id, git_sha=target.git_sha, image_tag=target.image_tag,
            profile=profile, status=DeploymentStatus.running, component=name,
            deploy_group_id=group_id, internal_port=target.internal_port,
            host_port=endpoints[name].port if get_settings().tier == "small" else None,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(rec)
        records[name] = rec
    db.commit()
    for rec in records.values():
        _mark_previous_stopped(db, rec)
    return records


class DeployInProgress(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"deployment already in progress for {name}")


class NoRollbackTarget(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"no previous successful deployment for {name}")
