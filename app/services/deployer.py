"""배포 오케스트레이터.

checkout → build(프로필별) → runtime 기동(1차 Docker / 2차 K8s) → (1차만) Caddy 전환.
프로젝트별 락으로 동시 배포를 1건으로 제한한다 (웹훅 연속 push 대비).
"""
import asyncio
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    BuildProfile, Deployment, DeploymentStatus, EnvVar, Project, ProjectType, RedirectRule,
)
from ..security import decrypt_value
from . import ports, proxy
from . import structure
from .build import (
    PROFILES,
    BuildError,
    build_image,
    checkout,
    docker_build_log_path,
    env_setup_log_path,
    install_dependencies,
    internal_port,
    write_start_script,
)
from .runtime import upstream_host
from .runtime.base import Endpoint, Runtime, RuntimeSpec

_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


def runtime_name() -> str:
    """지금 유효한 런타임 이름: k8s | windows_service | docker.

    PAAS_RUNTIME_BACKEND만 보면 틀린다 — enterprise 티어는 그 값과 무관하게 k8s다.
    화면·상태 보고가 이 판정을 다시 쓰기 때문에(services/monitor) get_runtime과 같은
    곳에서 이름만 돌려준다. 두 곳에 같은 분기를 쓰면 언젠가 갈라지고, 그러면 화면이
    실제로 도는 런타임과 다른 것을 말한다.
    """
    settings = get_settings()
    if settings.tier == "enterprise":
        return "k8s"
    if settings.runtime_backend == "windows_service":
        return "windows_service"
    return "docker"


# GPU를 배정할 수 있는 런타임. windows_service(nssm 네이티브 프로세스)에는 GPU 배정이라는
# 개념 자체가 없다 — 그 런타임은 RuntimeSpec.gpu를 읽지도 않는다.
GPU_CAPABLE_RUNTIMES = ("docker", "k8s")


def uses_native_runtime() -> bool:
    """이미지 대신 start.cmd로 도는 런타임인가.

    windows_service(nssm)와 dev 프로세스 런타임이 그렇다 — 둘 다 컴포넌트 폴더의 start.cmd를
    실행한다. 복합 배포가 이 경우에 build_image를 부르면 docker를 찾다 실패한다.
    """
    return get_settings().runtime_backend == "windows_service"


def uses_dev_process(profile: BuildProfile) -> bool:
    """개발 배포를 맨 프로세스로 띄우는가.

    development 프로필은 이미 dev 서버로 돈다(build.py가 만드는 start.cmd의 분기) — 그걸
    Windows Service로 감쌀 이유가 없다. 배포마다 nssm install/set/start, 정지 시 remove
    사이클이 돌고 서비스 등록에 관리자 권한이 필요한데, 정작 dev 서버는 HMR로 파일 변경을
    스스로 반영한다. URL rewrite는 그대로 필요하고, 그건 프록시와 start.cmd의 base가
    담당하므로 런타임을 바꿔도 달라지지 않는다.

    실행 스크립트가 start.cmd라 윈도우 전용이다. enterprise(k8s)는 제외한다 — 거기서
    개발 배포는 클러스터 안에서 도는 것이 전제이고 플랫폼 호스트의 프로세스가 아니다.
    """
    import os  # noqa: PLC0415

    return (
        profile == BuildProfile.development
        and os.name == "nt"
        and runtime_name() != "k8s"
    )


def _base_runtime() -> Runtime:
    name = runtime_name()
    if name == "k8s":
        from .runtime.k8s_runtime import K8sRuntime  # noqa: PLC0415

        return K8sRuntime()
    if name == "windows_service":
        from .runtime.windows_service_runtime import WindowsServiceRuntime  # noqa: PLC0415

        return WindowsServiceRuntime()
    from .runtime.docker_runtime import DockerRuntime  # noqa: PLC0415

    return DockerRuntime()


class ProfileRoutingRuntime(Runtime):
    """프로필에 따라 런타임을 갈라 주는 얇은 래퍼.

    **왜 get_runtime()에 프로필 인자를 두지 않는가.** 그러면 호출부 열 곳이 프로필을
    실어 나르게 되고(stop·status·logs·server-config·MCP·프리뷰·삭제 정리), 정작 필요한
    값은 이미 각 메서드 인자와 RuntimeSpec에 들어 있다. 라우팅을 여기 한 곳에 두면
    호출부는 그대로 두고 기동·정지·조회가 **자동으로 같은 런타임**을 보게 된다 — 기동은
    dev 프로세스로 하고 조회는 nssm에 물어 "없는 서비스"를 stopped라고 말하는 어긋남이
    구조적으로 생기지 않는다.
    """

    def _for(self, profile: BuildProfile) -> Runtime:
        if uses_dev_process(profile):
            from .runtime.dev_process_runtime import DevProcessRuntime  # noqa: PLC0415

            return DevProcessRuntime()
        return _base_runtime()

    def start(self, spec: RuntimeSpec) -> Endpoint:
        return self._for(spec.profile).start(spec)

    def stop(self, project_name: str, profile: BuildProfile) -> None:
        self._for(profile).stop(project_name, profile)

    def status(self, project_name: str, profile: BuildProfile) -> str:
        return self._for(profile).status(project_name, profile)

    def logs(self, project_name: str, profile: BuildProfile, tail: int = 200) -> str:
        return self._for(profile).logs(project_name, profile, tail)


def get_runtime() -> Runtime:
    return ProfileRoutingRuntime()


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


def _close_still_building(db: Session, records, error: BaseException | str) -> None:
    """아직 building인 배포 행을 실패로 닫는다.

    **왜 필요한가.** 실패 상태를 각 raise 자리에서 손으로 기록해 왔다. 그러다 예상 밖의
    예외(감지 이름 불일치로 인한 KeyError가 실측 사례다)가 나면 아무도 기록하지 않고,
    큐 작업은 그 예외를 삼킨다 — 행은 building으로 남고 화면은 영원히 "빌드 중"을 보여
    준다. 끝나지 않은 배포보다 실패한 배포가 낫다: 원인을 읽고 다시 시도할 수 있다.
    """
    text = str(error) or error.__class__.__name__
    closed = False
    for rec in list(records or []):
        if rec is None or rec.status != DeploymentStatus.building:
            continue
        rec.status = DeploymentStatus.failed
        rec.error = text[:2000]
        rec.finished_at = datetime.now(timezone.utc)
        closed = True
    if closed:
        db.commit()


def _close_orphaned_building(
    db: Session, project: Project, profile: BuildProfile, keep_ids: set[int],
) -> None:
    """이전 시도에서 building으로 남은 행을 닫는다 — 이번 배포의 행은 건드리지 않는다.

    플랫폼 프로세스가 배포 도중 재시작되면(자기 자신을 배포할 때가 그렇다) 진행 중이던
    행을 아무도 닫지 못한다. 그 행은 화면에서 계속 "빌드 중"으로 보이고, 사람은 끝나기를
    기다린다. 같은 프로필의 배포는 프로젝트 락이 직렬화하므로, 여기까지 왔다면 남아 있는
    building 행은 이번 시도의 것이 아니다.
    """
    rows = db.execute(
        select(Deployment).where(
            Deployment.project_id == project.id,
            Deployment.profile == profile,
            Deployment.status == DeploymentStatus.building,
        )
    ).scalars()
    _close_still_building(
        db, [r for r in rows if r.id not in keep_ids],
        "이전 배포 시도가 끝나지 않은 채 남아 있었습니다 "
        "(플랫폼 재시작 또는 예기치 못한 오류) — 이 시도는 취소된 것으로 봅니다.",
    )


def make_spec(
    db: Session, project: Project, image_tag: str, profile: BuildProfile,
    *, component: str | None = None, internal_port_override: int | None = None,
    work_subdir: str = "", base_path_relative: str = "",
) -> RuntimeSpec:
    """component가 주어지면(composite 전용) unit_name·이미지가 컴포넌트별로 분리되고,
    internal_port_override로 해당 컴포넌트의 실제 내부 포트를 지정한다(빌드 시점에
    감지된 타입 기준 — project.type은 composite 자체라 포트 매핑이 없다).

    work_subdir는 네이티브 런타임이 쓸 실행 폴더(컴포넌트 경로)다 — 이름이 아니라 경로다.
    base_path_relative는 그 컴포넌트가 프로젝트 주소 아래에서 받는 경로다(structure.routes_for) —
    dev 서버는 자기 공개 경로가 붙은 요청만 받으므로, 루트가 아닌 컴포넌트에 프로젝트 주소를
    주면 요청이 어긋난다."""
    from .host import gpu_allowed  # noqa: PLC0415

    settings = get_settings()
    port = internal_port_override if internal_port_override is not None else internal_port(project.type, profile)
    # 호스트 포트는 대장에서 받는다(services/ports.py) — 런타임이 그때그때 빈 포트를
    # 고르면 동시 배포가 같은 포트를 집고, 멈춘 배포의 포트가 남에게 넘어간다.
    # 2차(k8s)는 호스트 포트를 쓰지 않으므로 배정하지 않는다.
    host_port = None
    if settings.tier == "small":
        host_port = ports.allocate(
            db, project.id, profile, component or "",
            probe_host=upstream_host(settings),
        )
    return RuntimeSpec(
        project_name=project.name,
        image_tag=image_tag,
        internal_port=port,
        profile=profile,
        domain=proxy.domain_for(project.name, profile),
        base_path=proxy.path_prefix_for(
            _org_name(project), project.name, profile) + base_path_relative,
        env=resolve_env(db, project, profile),
        secret_keys=secret_env_keys(db, project),
        memory_limit=project.memory_limit or settings.default_memory_limit,
        cpu_limit=project.cpu_limit or settings.default_cpu_limit,
        replicas=PROFILES[profile].replicas,
        gpu=project.type.value == "llm" and gpu_allowed(),
        health_check_path=project.health_check_path,
        component=component,
        work_subdir=work_subdir,
        host_port=host_port,
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
        _close_orphaned_building(db, project, profile, {record.id} if record else set())
        try:
            assert_no_profile_conflict(project, profile)
        except ProfileConflict as e:
            if record is not None:
                record.status = DeploymentStatus.failed
                record.error = str(e)
                record.finished_at = datetime.now(timezone.utc)
                db.commit()
            raise
        workdir, sha = checkout(project, git_sha)
        # 배포 직전에 구조를 다시 읽는다. 저장된 구조로 빌드하면 리포가 바뀐 뒤에는 없는
        # 폴더를 찾거나 새 컴포넌트를 빼먹는다 — 빌드할 수 있는 것은 리포에 있는 것뿐이라
        # 감지 결과가 이긴다. 바뀐 내용은 감사기록에 남는다(services/structure.refresh).
        detected, _changed = structure.refresh(
            db, project, workdir, actor="deploy", source="repo", git_sha=sha)
        if project.type == ProjectType.composite:
            # 단일 배포 경로로 들어왔는데 구조가 복합이 됐다 — 여기서 계속하면 컴포넌트
            # 하나만 배포되고 나머지는 조용히 빠진다. 무엇이 감지됐는지 말하고 멈춘다.
            msg = (
                f"{project.name}: 리포 구조가 복합으로 감지됐습니다 "
                f"({structure.summary(detected)}) — 복합 배포로 다시 실행하세요."
            )
            if record is not None:
                record.status = DeploymentStatus.failed
                record.error = msg
                record.finished_at = datetime.now(timezone.utc)
                db.commit()
            raise BuildError(msg)
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
                write_start_script(workdir, project, profile=profile)
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
                # 외부에서 열리는 서브패스를 빌드에 넘긴다 — 프록시가 접두어를 벗겨
                # 넘기므로 앱은 "/"를 받지만 브라우저가 보는 주소는 서브패스다.
                install_dependencies(
                    workdir, log_path,
                    base_path=proxy.path_prefix_for(
                        _org_name(project), project.name, profile,
                    ),
                    # dev는 dev 서버가 소스를 즉석에서 서빙하므로 빌드 산출물을 쓰지 않는다.
                    build=profile != BuildProfile.development,
                )
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
                path_prefix = proxy.path_prefix_for(_org_name(project), project.name, profile)
                proxy.configure(
                    project.name, profile, spec.domain, path_prefix, endpoint,
                    redirects_for(db, project),
                    # dev는 빌드본이 아니라 dev 서버가 소스를 그대로 서빙한다. dev 서버는
                    # 자기 공개 경로(base)가 붙은 요청만 받으므로 접두사를 벗기면 안 된다
                    # (/@vite/client 같은 요청이 어긋난다). release는 반대로 벗겨야 한다 —
                    # 빌드된 HTML에 전체 경로가 박혀 있고 서버는 루트에서 서빙한다.
                    strip_prefix=profile != BuildProfile.development,
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
        except Exception as e:
            # 예상 밖의 예외도 레코드를 닫는다 — 큐 경로는 예외를 삼키므로, 닫지 않으면
            # 화면이 영원히 "빌드 중"에 머문다.
            _close_still_building(db, [record], e)
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

    # 레코드를 만들기 전에 막는다 — 만들고 나서 실패시키면 실패 이력만 쌓인다.
    assert_no_profile_conflict(project, profile)
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
    """composite 프로젝트의 비동기 배포 — deploy_queued와 동일한 패턴이나 컴포넌트마다
    자리 행을 미리 만들어 즉시 반환한다.

    자리 행의 이름은 **마지막에 감지된 구조**에서 받는다(structure.unit_names). 예전에는
    backend/frontend로 못 박았는데("컨벤션상 항상 이 둘"), 복합 배포가 일반화된 뒤로는
    사실이 아니다 — negowith(`api`+`web`)에서 배포 루프가 감지된 이름의 행을 찾다 KeyError로
    죽었고, 큐 작업이 그 예외를 삼켜 화면은 영원히 building이었다(실측).

    큐에 올린 뒤 체크아웃하면 구조가 달라질 수도 있으므로 이름이 맞는다고 믿지 않는다 —
    deploy_composite_sync가 감지 결과와 자리 행을 맞춘다."""
    from ..db import SessionLocal  # noqa: PLC0415
    from . import jobs  # noqa: PLC0415

    assert_no_profile_conflict(project, profile)
    records = {
        name: Deployment(
            project_id=project.id, git_sha=git_sha or "", image_tag="", profile=profile,
            status=DeploymentStatus.building, component=name,
        )
        for name in structure.unit_names(project.structure)
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
        path_prefix = proxy.path_prefix_for(_org_name(project), project.name, profile)
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
        _close_orphaned_building(
            db, project, profile, {r.id for r in (records or {}).values() if r.id})
        assert_no_profile_conflict(project, profile)
        workdir, sha = checkout(project, git_sha)
        # 단일 배포와 같은 이유로 구조를 다시 읽어 저장한다 — 화면·감사기록이 실제로
        # 배포한 구성을 말해야 한다.
        detected, _changed = structure.refresh(
            db, project, workdir, actor="deploy", source="repo", git_sha=sha)
        # 감지된 구조가 배포 명세다 — 폴더 이름이 backend/frontend가 아니어도, 컴포넌트가
        # 셋 이상이어도 그대로 배포한다(services/structure). 예전에는 그 두 이름만 다뤘다.
        components = {
            str(c["name"]): (c["path"], ProjectType(c["type"]))
            for c in (detected.get("components") or [])
            if c.get("type")
        }
        if len(components) < 2:
            raise BuildError(
                f"{project.name}: 복합 배포에는 타입이 판정된 컴포넌트가 둘 이상 필요합니다 "
                f"(감지된 구조: {structure.summary(detected) or '없음'})"
            )
        if structure.missing_root_component(detected):
            # 루트를 받는 컴포넌트가 없으면 프로젝트 주소 자체가 404가 된다. 조용히
            # 배포하면 "성공했는데 주소가 열리지 않는" 상태가 된다.
            raise BuildError(
                f"{project.name}: 프로젝트 주소(루트)를 받을 컴포넌트를 정할 수 없습니다 "
                f"(감지된 구조: {structure.summary(detected)}). 화면 컴포넌트를 "
                "frontend로 두거나 react·html 타입이 하나만 되도록 정리하세요."
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
            # 큐에 올릴 때 만든 자리 행은 **그때** 알던 구조 기준이다. 체크아웃 후 감지한
            # 구조가 진짜 배포 명세이므로 어긋나면 맞춘다 — 예전에는 아래 루프의
            # records[name]에서 KeyError가 나고, 큐 작업이 그 예외를 삼켜 자리 행이
            # building으로 영원히 남았다(실측: negowith는 api/web인데 자리 행은
            # backend/frontend였다).
            for name in components:
                if name not in records:
                    records[name] = Deployment(
                        project_id=project.id, git_sha=sha, image_tag="", profile=profile,
                        status=DeploymentStatus.building, component=name,
                    )
                    db.add(records[name])
            for name in [n for n in records if n not in components]:
                stale = records.pop(name)
                stale.status = DeploymentStatus.failed
                stale.error = (
                    f"'{name}' 컴포넌트는 지금 리포에서 감지되지 않습니다 "
                    f"(감지된 구성: {structure.summary(detected)}) — 이번 배포에서 제외했습니다."
                )
                stale.finished_at = datetime.now(timezone.utc)
            for rec in records.values():
                rec.git_sha = sha
                rec.deploy_group_id = group_id
        db.commit()

        endpoints: dict[str, Endpoint] = {}
        failed_component: str | None = None
        failure: Exception | None = None
        # 컴포넌트가 외부에서 열리는 경로 — 프록시 라우팅과 **같은 원천**에서 받는다
        # (structure.routes_for). 프런트엔드 빌드에 넘기는 --base가 이 경로와 다르면
        # HTML이 없는 주소로 자산을 참조해 제목만 뜨는 빈 화면이 된다.
        relatives = dict(structure.routes_for(detected))
        for name, (comp_path, comp_type) in components.items():
            rec = records[name]
            # 이 컴포넌트의 빌드를 시작하기 전에 로그 경로를 커밋한다 — build_image 호출이
            # 오래 걸리거나 멈춰도 그 시점까지의 진행 상황을 조회할 수 있어야 한다.
            rec.build_log_path = str(docker_build_log_path(project.name, sha, profile, name))
            db.commit()
            try:
                if uses_native_runtime():
                    # 네이티브 런타임은 이미지를 만들지 않는다 — 컴포넌트 폴더에 그 컴포넌트의
                    # start.cmd를 쓰고, 그 폴더에서 설치·기동한다. 예전에는 "리포 루트의 단일
                    # start.cmd만 실행한다"며 복합을 거부했는데, 스크립트를 컴포넌트마다 두면
                    # 유닛·포트·공개 경로가 이미 컴포넌트별인 구조와 아귀가 맞는다.
                    comp_dir = workdir / comp_path if comp_path else workdir
                    write_start_script(comp_dir, project, component=name, profile=profile)
                    rec.internal_port = internal_port(comp_type, profile)
                    rec.build_log_path = str(
                        env_setup_log_path(f"{project.name}-{name}", sha, profile))
                    db.commit()
                    install_dependencies(
                        comp_dir, Path(rec.build_log_path),
                        base_path=proxy.path_prefix_for(
                            _org_name(project), project.name, profile,
                        ) + relatives.get(name, ""),
                        build=profile != BuildProfile.development,
                    )
                    spec = make_spec(
                        db, project, "", profile, component=name,
                        internal_port_override=rec.internal_port, work_subdir=comp_path,
                        base_path_relative=relatives.get(name, ""),
                    )
                else:
                    result = build_image(
                        project, workdir, sha, profile, component=name,
                        component_type=comp_type, context_subdir=comp_path,
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
                domain = proxy.domain_for(project.name, profile)
                base_prefix = proxy.path_prefix_for(
                    _org_name(project), project.name, profile,
                )
                # 컴포넌트별 공개 경로는 한 곳에서 정한다(structure.routes_for) — backend는
                # api/, frontend는 루트를 그대로 지키고(이미 배포된 주소다) 그 밖은 이름을
                # 경로로 쓴다. 긴 경로가 먼저 오도록 이미 정렬돼 있다: 루트("")가 앞에 오면
                # 그 뒤 규칙이 전부 가려진다.
                routes = [
                    proxy.PathRoute(
                        path_prefix=base_prefix + relative, endpoint=endpoints[name],
                    )
                    for name, relative in structure.routes_for(detected)
                    if name in endpoints
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
    except Exception as e:
        # 여기까지 오는 예외가 레코드에 기록됐다는 보장은 없다 — 기록은 각 raise 자리에서
        # 손으로 하기 때문이다. 남은 building 행을 닫고 예외는 그대로 올린다.
        _close_still_building(db, (records or {}).values(), e)
        raise
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
        domain = proxy.domain_for(project.name, profile)
        base_prefix = proxy.path_prefix_for(_org_name(project), project.name, profile)
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


class ProfileConflict(RuntimeError):
    def __init__(self, project_name: str, other: BuildProfile):
        super().__init__(
            f"{project_name}: {other.value} 프로필이 이미 떠 있습니다. 두 프로필은 같은 "
            f"도메인에서 경로가 겹쳐(/apps/.../ 규칙이 그 아래 /dev/까지 함께 잡습니다) "
            f"동시에 띄울 수 없습니다 — {other.value}를 먼저 중지한 뒤 배포하세요."
        )


def assert_no_profile_conflict(project: Project, profile: BuildProfile) -> None:
    """release와 development를 동시에 띄우지 못하게 막는다.

    1차(small)는 두 프로필이 같은 도메인을 쓰고 경로만 다르다(/apps/{조직}/{프로젝트}/ 와
    그 아래 /dev/). 프록시 규칙은 접두사 매칭이라 release 규칙이 dev 경로까지 함께 잡고,
    둘이 동시에 떠 있으면 어느 쪽이 응답할지 규칙 순서에 달린다 — 주소가 충돌한다.
    2차(enterprise)는 프로필마다 도메인이 갈리므로 해당 없다.
    """
    if get_settings().tier != "small":
        return
    other = (
        BuildProfile.release if profile == BuildProfile.development
        else BuildProfile.development
    )
    try:
        other_status = get_runtime().status(project.name, other)
    except Exception:
        # 상태를 못 읽는 것("알 수 없음")을 충돌로 처리하면 안 된다 — 런타임 조회가
        # 잠깐 실패했다는 이유로 멀쩡한 배포가 막힌다. 이 가드는 주소 충돌을 줄이려는
        # 것이지 안전 필수 불변식이 아니므로, 판단이 안 서면 통과시킨다.
        return
    if other_status == "running":
        raise ProfileConflict(project.name, other)


class DeployInProgress(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"deployment already in progress for {name}")


class NoRollbackTarget(RuntimeError):
    def __init__(self, name: str):
        super().__init__(f"no previous successful deployment for {name}")
