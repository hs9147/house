"""임시 작업 — 한동안만 돌리고 나중에 걷어 낼 운영 스크립트(대시보드 「시스템 상태」의 「임시 작업」).

스크립트는 **코드에 둔다**(TASKS). 콘솔에서 임의 코드를 쓰고 돌리게 하면 관리자 화면 하나가 서버의 모든 권한이
된다 — 화면에서는 켜고 끄기·지금 실행·현황·이력만 한다. 다 쓰면 TASKS에서 지운다(스케줄러 job은 reconcile이
지운다).

돌리는 때는 둘이다: 일이 생길 때(이벤트 — 예: 가입 요청 user.register) 바로, 그리고 스케줄러가 주기마다(놓친 것·
이미 쌓여 있던 것). 둘 다 같은 함수다. 현황은 스케줄러 job 행(마지막 실행·결과·켜짐), 이력은 **실제로 무언가를
했을 때만** 감사 기록(action tempjob.{id})에 남긴다 — 매번 남기면 할 일이 없었던 실행이 이력을 덮는다.
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..models import JobKind, Organization, ScheduledJob, UserAccount

INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class TempJob:
    id: str
    title: str
    description: str
    run: Callable[[Session], dict]       # → {"done": 건수, …} — done이 0이면 할 일이 없었다
    events: tuple[str, ...] = field(default=())


# --- 사용자 등록 즉시 승인 · 소속 없는 계정에 gp3 ---
# 둘로 나눈다 — 승인은 그대로 두고 부서만 자동으로 넣거나, 그 반대로 쓸 수 있게(대시보드에서 따로 켜고 끈다).

DEFAULT_ORG = "gp3"


def _auto_approve(db: Session) -> dict:
    """승인 대기 계정을 승인한다(부서는 건드리지 않는다 — auto_org)."""
    pending = list(db.execute(select(UserAccount).where(UserAccount.is_approved.is_(False))).scalars())
    if not pending:
        return {"done": 0}
    for user in pending:
        user.is_approved = True
    db.commit()
    return {"done": len(pending), "approved": [u.email for u in pending]}


def _auto_org(db: Session) -> dict:
    """소속 조직(부서)이 하나도 없는 계정에 조직 gp3을 붙이고 기본 조직으로 둔다(승인 여부와 무관 — 대기 계정도).
    이미 다른 부서가 있는 계정에는 더하지 않는다. 조직이 없으면 아무것도 하지 않고 그 사실을 남긴다 — 조직은
    관리자가 만든다(Gitea 조직이 함께 생기는 일이라 스크립트가 만들지 않는다)."""
    from ..api.system import _sync_gitea_membership  # noqa: PLC0415 — 조직 소속을 Gitea 팀에도(화면의 배지와 같다)

    orphans = [u for u in db.execute(select(UserAccount)).scalars() if not u.organizations]
    if not orphans:
        return {"done": 0}
    org = db.execute(select(Organization).where(Organization.name == DEFAULT_ORG)).scalar_one_or_none()
    if org is None:
        # 이력은 남기지 않는다(done 0) — 1분마다 같은 말이 쌓인다. 현황(마지막 실행 결과)에는 보인다.
        return {"done": 0, "waiting": len(orphans),
                "note": f"조직 {DEFAULT_ORG}이 없어 부서를 넣지 못했습니다 — 조직을 만들면 다음 실행에 넣습니다"}
    for user in orphans:
        user.organizations.append(org)
        if user.organization_id is None:
            user.organization_id = org.id
    db.commit()
    for user in orphans:
        _sync_gitea_membership(db, "tempjob:auto_org", user, org, member=True)
    return {"done": len(orphans), "joined": [u.email for u in orphans], "organization": DEFAULT_ORG}


TASKS: dict[str, TempJob] = {t.id: t for t in (
    TempJob(id="auto_approve", title="사용자 등록 즉시 승인",
            description="가입 요청을 바로 승인한다. "
                        "가입할 때 바로, 그리고 1분마다 남은 대기 계정을.",
            run=_auto_approve, events=("user.register",)),
    TempJob(id="auto_org", title=f"소속 없는 계정에 {DEFAULT_ORG}",
            description=f"부서(조직)가 하나도 없는 계정에 조직 {DEFAULT_ORG}을 붙이고 기본 조직으로 둔다 — 다른 부서가 "
                        "있는 계정은 건드리지 않는다. 가입할 때 바로, 그리고 1분마다.",
            run=_auto_org, events=("user.register",)),
)}


# --- 실행·현황 ---

def job_of(db: Session, task_id: str) -> ScheduledJob | None:
    from . import scheduler  # noqa: PLC0415

    return db.execute(select(ScheduledJob).where(
        ScheduledJob.name == scheduler.job_name(JobKind.temp_job, task_id))).scalar_one_or_none()


def run(db: Session, task_id: str, actor: str = "scheduler") -> dict:
    """하나를 돌린다 — 무언가를 했으면 이력(감사)에 남긴다. 예외는 올린다(스케줄러·API가 결과로 바꾼다)."""
    task = TASKS[task_id]
    result = task.run(db)
    if result.get("done"):
        audit.record(db, actor, f"tempjob.{task_id}", task.title, result)
    return result


def on_event(db: Session, event: str) -> None:
    """일이 생겼을 때 — 그 이벤트를 받는 켜진 임시 작업을 바로 돌린다. 실패는 삼킨다(가입 자체는 이미 끝났다 —
    남은 것은 1분 뒤 주기 실행이 다시 한다)."""
    for task in TASKS.values():
        if event not in task.events:
            continue
        job = job_of(db, task.id)
        if job is None or not job.enabled:
            continue   # 스케줄러에 올라가 켜져 있는 것만 — 끄기가 곧 멈춤이다(기동 후 첫 tick 전이면 주기 실행이 줍는다)
        try:
            run(db, task.id, actor=f"event:{event}")
        except Exception:  # noqa: BLE001
            db.rollback()


def listing(db: Session, history: int = 30) -> list[dict]:
    """화면용 — 작업마다 설명·현황(스케줄러 job)·최근 이력(감사)."""
    from ..models import AuditEvent  # noqa: PLC0415

    out = []
    for task in TASKS.values():
        job = job_of(db, task.id)
        rows = db.execute(select(AuditEvent).where(AuditEvent.action == f"tempjob.{task.id}")
                          .order_by(AuditEvent.id.desc()).limit(history)).scalars()
        out.append({
            "id": task.id, "title": task.title, "description": task.description, "events": list(task.events),
            "job": None if job is None else {
                "id": job.id, "enabled": job.enabled, "interval_seconds": job.interval_seconds,
                "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
                "last_status": job.last_status.value if job.last_status else None,
                "last_detail": job.last_detail, "consecutive_failures": job.consecutive_failures},
            "history": [{"at": r.created_at.isoformat(), "actor": r.actor, "detail": r.detail} for r in rows],
        })
    return out
