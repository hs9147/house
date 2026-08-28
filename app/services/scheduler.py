"""주기 갱신 스케줄러 — 플랫폼이 이미 하던 일들을 한 곳에서 돌리고 결과를 남긴다.

**왜 모았나.** 갱신이 필요한 일이 넷인데 다루는 방식이 제각각이었다:

  외부 API 카탈로그  자기 데몬 스레드 하나(24시간 하드코딩, 결과를 아무도 못 봄)
  문서 색인·온톨로지  스케줄러 없음 — 사람이 눌러야 돌았다
  mcp 모듈 응답 확인  모듈 화면 버튼으로만
  external_api 확인   없음

각각이 자기 스레드를 갖게 두면 "무엇이 언제 돌았고 왜 실패했나"에 답할 곳이 끝내
생기지 않는다. 스레드 하나가 표 하나를 보고 돌면, 그 표가 곧 답이다(대시보드 모니터).

**작업 목록은 사람이 만들지 않는다.** reconcile()이 지금 있는 것에서 만들어 낸다 —
저장소가 늘면 job이 늘고, 모듈을 지우면 job이 사라진다. 손으로 관리하면 없는 대상을
가리키는 job이 남아 영원히 실패로 뜬다(사내 MCP 목록을 그렇게 만든 이유와 같다).

**한 번에 하나씩, 순서대로 돈다.** 병렬로 돌리면 문서 색인 여러 건이 동시에 추출을
해서 서버를 먹는데, 이 일들은 급하지 않다 — 늦게 끝나도 다음 tick에서 이어진다.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import JobKind, JobStatus, Module, ModuleType, ScheduledJob, utcnow

# tick 간격. job의 주기와 다르다 — tick마다 "지금 돌 때가 된 job"만 고른다.
TICK_SECONDS = 30.0
# 기동 직후를 피한다. 첫 요청이 몰리는 구간에 문서 추출을 끼워 넣지 않는다.
STARTUP_DELAY = 20.0

# 종류별 기본 주기. 값의 근거는 "바뀌는 속도"다 — 카탈로그는 하루 단위로 바뀌고,
# 사내 문서는 사람이 저장하는 즉시 바뀌며, 서버가 죽은 것은 빨리 알아야 한다.
DEFAULT_INTERVALS = {
    JobKind.api_catalog: 86_400,
    JobKind.doc_index: 900,
    JobKind.mcp_probe: 600,
    JobKind.api_probe: 600,
}

_lock = threading.Lock()
_thread: threading.Thread | None = None


def job_name(kind: JobKind, target: str = "") -> str:
    return f"{kind.value}:{target}" if target else kind.value


def _wanted(db: Session) -> list[tuple[JobKind, str]]:
    """지금 돌아야 하는 일의 목록 — 설정과 등록 현황에서 만든다."""
    from . import storage  # noqa: PLC0415 — 순환 import 회피

    wanted: list[tuple[JobKind, str]] = [(JobKind.api_catalog, "")]
    try:
        # 문서 색인은 숨긴 저장소(internal)까지 전부 돈다 — /mcp/docs가 검색하는 범위와 같다.
        wanted += [(JobKind.doc_index, s.name) for s in storage.stores()]
    except storage.StorageError:
        pass  # 환경변수가 잘못돼 있으면 문서 job만 빠지고 나머지는 그대로 돈다
    for module in db.execute(select(Module).order_by(Module.name)).scalars():
        if module.type == ModuleType.mcp:
            wanted.append((JobKind.mcp_probe, module.name))
        elif module.type == ModuleType.external_api:
            wanted.append((JobKind.api_probe, module.name))
    return wanted


def reconcile(db: Session) -> dict:
    """작업 표를 현실에 맞춘다. 이미 있는 행의 설정(주기·enabled)은 건드리지 않는다."""
    names = {job_name(kind, target): (kind, target) for kind, target in _wanted(db)}
    existing = {j.name: j for j in db.execute(select(ScheduledJob)).scalars()}

    added = 0
    for name, (kind, target) in names.items():
        if name in existing:
            continue
        db.add(ScheduledJob(
            name=name, kind=kind, target=target,
            interval_seconds=DEFAULT_INTERVALS[kind],
        ))
        added += 1
    removed = [j for name, j in existing.items() if name not in names]
    for job in removed:
        db.delete(job)
    db.commit()
    return {"added": added, "removed": len(removed), "total": len(names)}


def due(db: Session, now: datetime | None = None) -> list[ScheduledJob]:
    """지금 돌 때가 된 job. 한 번도 안 돈 job이 먼저다."""
    now = now or datetime.now(timezone.utc)
    out = []
    for job in db.execute(
        select(ScheduledJob).where(ScheduledJob.enabled.is_(True))
    ).scalars():
        last = job.last_run_at
        if last is None:
            out.append(job)
            continue
        if last.tzinfo is None:  # sqlite는 tz 없이 돌려준다
            last = last.replace(tzinfo=timezone.utc)
        if now - last >= timedelta(seconds=job.interval_seconds):
            out.append(job)
        elif isinstance(job.last_detail, dict) and job.last_detail.get("done") is False:
            # 한 번에 못 끝낸 작업은 다음 주기까지 기다리지 않는다. 색인은 호출마다
            # 예산(20초)만큼만 진행하고 `done: false`로 답하는데, 그걸 15분 재우면
            # 듀티 사이클이 2%다 — 문서가 몇 천 건인 저장소는 첫 색인에 하루가 걸린다.
            # 예전에는 사람이 reindex_docs를 done이 될 때까지 다시 눌러서 안 드러났다.
            out.append(job)
    return sorted(out, key=lambda j: (j.last_run_at is not None, j.last_run_at or j.id))


def run(db: Session, job: ScheduledJob) -> dict:
    """job 하나를 지금 돌리고 결과를 행에 남긴다. **예외를 올리지 않는다** —
    한 job이 실패해도 다음 job은 돌아야 하고, 실패는 표에 적히는 것이 목적이다."""
    started = time.monotonic()
    try:
        status, detail = _dispatch(db, job)
    except Exception as e:  # noqa: BLE001 — 어떤 실패든 표에 남긴다
        status, detail = JobStatus.failed, {"error": f"{type(e).__name__}: {e}"[:500]}

    job.last_run_at = utcnow()
    job.last_status = status
    job.last_ms = int((time.monotonic() - started) * 1000)
    job.last_detail = detail
    job.consecutive_failures = (
        job.consecutive_failures + 1 if status == JobStatus.failed else 0
    )
    db.commit()
    return {"job": job.name, "status": status.value, "ms": job.last_ms, "detail": detail}


def _dispatch(db: Session, job: ScheduledJob) -> tuple[JobStatus, dict]:
    from . import apisearch, docsearch, mcp_client  # noqa: PLC0415
    from . import modules as modules_service  # noqa: PLC0415
    from . import storage  # noqa: PLC0415

    if job.kind == JobKind.api_catalog:
        result = apisearch.sync_catalog(db)
        changed = result["added"] + result["updated"] + result["removed"]
        return (JobStatus.ok if changed else JobStatus.skipped), result

    if job.kind == JobKind.doc_index:
        store = storage.store(job.target)
        if store is None:
            return JobStatus.failed, {"error": f"저장소가 없습니다: {job.target}"}
        result = docsearch.reindex(store.name, store.root)
        changed = result["indexed"] + result["failed"] + result["removed"]
        return (JobStatus.ok if changed else JobStatus.skipped), result

    module = db.execute(
        select(Module).where(Module.name == job.target)
    ).scalar_one_or_none()
    if module is None:
        return JobStatus.failed, {"error": f"모듈이 없습니다: {job.target}"}
    config = modules_service.decrypt_config(module.config or {})

    if job.kind == JobKind.mcp_probe:
        result = mcp_client.check_server(config.get("url", ""), config.get("api_key"))
        return (JobStatus.ok if result["ok"] else JobStatus.failed), {
            "tool_count": result.get("tool_count", 0), "error": result.get("error"),
        }

    # api_probe — 등록된 주소가 살아 있는지만 본다. 응답 본문은 쓰지 않는다:
    # 무엇을 어디에 저장할지는 모듈마다 다르고, 그것을 지어내면 틀린 값이 DB에 쌓인다.
    from .httpx_retry import get_with_retry  # noqa: PLC0415

    url = config.get("url", "")
    if not url:
        return JobStatus.failed, {"error": "url이 비어 있습니다."}
    res = get_with_retry(url, timeout=10)
    return (
        JobStatus.ok if res.status_code < 400 else JobStatus.failed,
        {"status_code": res.status_code, "url": url},
    )


def tick(db: Session) -> dict:
    """한 번의 순회 — 재조정하고, 밀린 job을 순서대로 돌린다."""
    reconciled = reconcile(db)
    ran = [run(db, job) for job in due(db)]
    return {"reconciled": reconciled, "ran": ran}


def start() -> None:
    """스케줄러 스레드를 띄운다. 이미 살아 있으면 아무 일도 하지 않는다."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return

        def _loop():
            from ..db import SessionLocal  # noqa: PLC0415

            time.sleep(STARTUP_DELAY)
            while True:
                db = SessionLocal()
                try:
                    tick(db)
                except Exception:  # noqa: BLE001 — 스케줄러가 죽으면 갱신이 통째로 멈춘다
                    pass
                finally:
                    db.close()
                time.sleep(TICK_SECONDS)

        _thread = threading.Thread(target=_loop, daemon=True, name="paas-scheduler")
        _thread.start()


def snapshot(db: Session) -> dict:
    """대시보드 모니터가 묻는 것 — 지금 무엇이 밀려 있고 무엇이 죽어 있나."""
    jobs = list(db.execute(select(ScheduledJob).order_by(ScheduledJob.name)).scalars())
    now = datetime.now(timezone.utc)
    return {
        "running": _thread is not None and _thread.is_alive(),
        "tick_seconds": TICK_SECONDS,
        "failing": sum(1 for j in jobs if j.consecutive_failures > 0),
        "never_run": sum(1 for j in jobs if j.last_run_at is None),
        "jobs": [
            {
                "id": j.id, "name": j.name, "kind": j.kind.value, "target": j.target,
                "enabled": j.enabled, "interval_seconds": j.interval_seconds,
                "last_run_at": j.last_run_at,
                "last_status": j.last_status.value if j.last_status else None,
                "last_ms": j.last_ms, "last_detail": j.last_detail,
                "consecutive_failures": j.consecutive_failures,
                "overdue": j in due(db, now),
            }
            for j in jobs
        ],
    }
