"""주기 갱신 스케줄러 — 작업 표를 현실에서 만들고, 밀린 것만 돌리고, 결과를 남긴다.

스케줄러 스레드 자체는 conftest에서 꺼 둔다(무작위 시각에 test-paas.db에 쓰면 어느
테스트든 흔들 수 있다). 여기서는 tick/run/due를 직접 불러 본다.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.models import JobKind, JobStatus, Module, ModuleType, ScheduledJob, utcnow
from app.services import httpx_retry, scheduler

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


@pytest.fixture
def db(monkeypatch, tmp_path):
    """표를 만들고 세션 하나를 연다. 문서 저장소는 tmp_path 하나로 고정한다 —
    호스트에 무엇이 깔려 있든 job 개수가 흔들리지 않게."""
    from app.db import SessionLocal

    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_ROOTS", "")
    get_settings.cache_clear()
    create_app()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        get_settings.cache_clear()


def _names(db) -> set[str]:
    return {j.name for j in db.query(ScheduledJob).all()}


# --- 작업 목록은 현실에서 만들어진다 ---

def test_reconcile_builds_jobs_from_registered_things(db, fresh_settings):
    scheduler.reconcile(db)
    # 카탈로그 하나 + 숨긴 internal 저장소 하나. 모듈이 없으니 probe도 없다.
    assert _names(db) == {"api_catalog", "doc_index:internal"}

    db.add_all([
        Module(name="mcp-ops", type=ModuleType.mcp, config={"url": "http://x/mcp"}),
        Module(name="news-api", type=ModuleType.external_api, config={"url": "http://y/"}),
        Module(name="shop-db", type=ModuleType.database, config={}),
    ])
    db.commit()

    result = scheduler.reconcile(db)
    assert result["added"] == 2  # database 모듈은 갱신할 것이 없다
    assert _names(db) == {
        "api_catalog", "doc_index:internal", "mcp_probe:mcp-ops", "api_probe:news-api",
    }


def test_reconcile_removes_orphans_but_keeps_settings(db, fresh_settings):
    db.add(Module(name="news-api", type=ModuleType.external_api, config={"url": "http://y/"}))
    db.commit()
    scheduler.reconcile(db)

    # 사람이 끈 job은 재조정을 넘어 살아남는다 — 껐는데 다음 tick에 되살아나면 끈 것이 아니다.
    job = db.query(ScheduledJob).filter_by(name="api_probe:news-api").one()
    job.enabled = False
    job.interval_seconds = 4242
    db.commit()

    scheduler.reconcile(db)
    db.expire_all()
    job = db.query(ScheduledJob).filter_by(name="api_probe:news-api").one()
    assert job.enabled is False
    assert job.interval_seconds == 4242

    db.delete(db.query(Module).filter_by(name="news-api").one())
    db.commit()
    assert scheduler.reconcile(db)["removed"] == 1
    assert "api_probe:news-api" not in _names(db)


def test_doc_index_job_per_store(db, monkeypatch, tmp_path, fresh_settings):
    (tmp_path / "rules").mkdir()
    monkeypatch.setenv("PAAS_DOC_ROOTS", f"rules={tmp_path / 'rules'}")
    get_settings.cache_clear()

    scheduler.reconcile(db)
    assert "doc_index:rules" in _names(db)


# --- 밀린 것만 돈다 ---

def test_due_prefers_never_run_and_respects_interval(db, fresh_settings):
    scheduler.reconcile(db)
    jobs = {j.name: j for j in db.query(ScheduledJob).all()}
    assert {j.name for j in scheduler.due(db)} == set(jobs)  # 다 한 번도 안 돌았다

    catalog = jobs["api_catalog"]
    catalog.last_run_at = utcnow()
    db.commit()
    due = scheduler.due(db)
    assert catalog not in due
    # 한 번도 안 돈 job이 먼저 온다
    assert due[0].last_run_at is None

    catalog.last_run_at = utcnow() - timedelta(seconds=catalog.interval_seconds + 1)
    db.commit()
    assert catalog in scheduler.due(db)

    catalog.enabled = False
    db.commit()
    assert catalog not in scheduler.due(db)


# --- 실행 결과는 반드시 표에 남는다 ---

def test_run_records_failure_without_raising(db, fresh_settings):
    """대상이 사라진 job이 예외를 올리면 그 tick의 뒤 job이 통째로 안 돈다."""
    job = ScheduledJob(name="api_probe:gone", kind=JobKind.api_probe, target="gone",
                       interval_seconds=600)
    db.add(job)
    db.commit()

    result = scheduler.run(db, job)
    assert result["status"] == "failed"
    assert "모듈이 없습니다" in result["detail"]["error"]
    assert job.consecutive_failures == 1
    assert job.last_run_at is not None
    assert job.last_ms is not None

    scheduler.run(db, job)
    assert job.consecutive_failures == 2


def test_run_captures_exceptions_from_the_work_itself(db, monkeypatch, fresh_settings):
    """수집이 터져도 뒤에 밀린 문서 색인은 돌아야 한다 — 예외는 표에 적히고 끝난다."""
    from app.services import apisearch

    scheduler.reconcile(db)
    job = db.query(ScheduledJob).filter_by(name="api_catalog").one()

    def _boom(_db, **kw):
        raise RuntimeError("디렉터리 응답이 JSON이 아님")

    monkeypatch.setattr(apisearch, "sync_catalog", _boom)
    result = scheduler.run(db, job)
    assert result["status"] == "failed"
    assert result["detail"]["error"] == "RuntimeError: 디렉터리 응답이 JSON이 아님"


def test_api_probe_uses_module_url_and_clears_failures(db, monkeypatch, fresh_settings):
    db.add(Module(name="news-api", type=ModuleType.external_api,
                  config={"url": "http://news.internal/health"}))
    db.commit()
    scheduler.reconcile(db)
    job = db.query(ScheduledJob).filter_by(name="api_probe:news-api").one()
    job.consecutive_failures = 3
    db.commit()

    called = {}

    class _R:
        status_code = 200

    httpx_retry.reset_breakers()
    monkeypatch.setattr(httpx_retry.httpx, "get",
                        lambda url, **kw: (called.update(url=url), _R())[1])

    result = scheduler.run(db, job)
    assert called["url"] == "http://news.internal/health"
    assert result["status"] == "ok"
    assert result["detail"] == {"status_code": 200, "url": "http://news.internal/health"}
    assert job.consecutive_failures == 0


def test_api_probe_marks_error_status_as_failed(db, monkeypatch, fresh_settings):
    db.add(Module(name="news-api", type=ModuleType.external_api,
                  config={"url": "http://news.internal/health"}))
    db.commit()
    scheduler.reconcile(db)
    job = db.query(ScheduledJob).filter_by(name="api_probe:news-api").one()

    class _R:
        status_code = 503

    httpx_retry.reset_breakers()
    monkeypatch.setattr(httpx_retry.httpx, "get", lambda url, **kw: _R())
    assert scheduler.run(db, job)["status"] == "failed"


def test_mcp_probe_reports_tool_count(db, monkeypatch, fresh_settings):
    from app.services import mcp_client

    db.add(Module(name="mcp-ops", type=ModuleType.mcp,
                  config={"url": "http://paas.internal/mcp/ops"}))
    db.commit()
    scheduler.reconcile(db)
    job = db.query(ScheduledJob).filter_by(name="mcp_probe:mcp-ops").one()

    monkeypatch.setattr(mcp_client, "check_server",
                        lambda url, key=None: {"ok": True, "tool_count": 7})
    result = scheduler.run(db, job)
    assert result["status"] == "ok"
    assert result["detail"]["tool_count"] == 7

    monkeypatch.setattr(mcp_client, "check_server",
                        lambda url, key=None: {"ok": False, "error": "연결 실패"})
    assert scheduler.run(db, job)["status"] == "failed"


def test_catalog_job_reports_skipped_when_nothing_changed(db, monkeypatch, fresh_settings):
    from app.services import apisearch

    scheduler.reconcile(db)
    job = db.query(ScheduledJob).filter_by(name="api_catalog").one()

    monkeypatch.setattr(apisearch, "sync_catalog",
                        lambda _db, **kw: {"added": 0, "updated": 0, "removed": 0})
    # 바뀐 것이 없는 것은 실패가 아니다 — 실패로 적으면 모니터가 항상 빨갛다.
    assert scheduler.run(db, job)["status"] == "skipped"

    monkeypatch.setattr(apisearch, "sync_catalog",
                        lambda _db, **kw: {"added": 3, "updated": 0, "removed": 0})
    assert scheduler.run(db, job)["status"] == "ok"


def test_tick_reconciles_then_runs_due_jobs(db, monkeypatch, fresh_settings):
    from app.services import apisearch, docsearch

    monkeypatch.setattr(apisearch, "sync_catalog",
                        lambda _db, **kw: {"added": 1, "updated": 0, "removed": 0})
    monkeypatch.setattr(docsearch, "reindex",
                        lambda name, root: {"indexed": 0, "failed": 0, "removed": 0})

    result = scheduler.tick(db)
    assert result["reconciled"]["added"] == 2
    assert {r["job"] for r in result["ran"]} == {"api_catalog", "doc_index:internal"}
    # 방금 돌았으니 다음 tick에서는 아무것도 안 돈다
    assert scheduler.tick(db)["ran"] == []


# --- 모니터가 읽는 값 ---

def test_snapshot_counts_failing_and_overdue(db, fresh_settings):
    scheduler.reconcile(db)
    snap = scheduler.snapshot(db)
    assert snap["running"] is False  # conftest가 스레드를 안 띄운다
    assert snap["tick_seconds"] == scheduler.TICK_SECONDS
    assert snap["never_run"] == len(snap["jobs"]) == 2
    assert snap["failing"] == 0
    assert all(j["overdue"] for j in snap["jobs"])

    job = db.query(ScheduledJob).filter_by(name="api_catalog").one()
    job.last_run_at = utcnow()
    job.last_status = JobStatus.failed
    job.consecutive_failures = 2
    db.commit()

    snap = scheduler.snapshot(db)
    assert snap["failing"] == 1
    assert snap["never_run"] == 1
    row = next(j for j in snap["jobs"] if j["name"] == "api_catalog")
    assert row["overdue"] is False
    assert row["last_status"] == "failed"
    assert row["consecutive_failures"] == 2


# --- REST 창구 ---

def test_scheduler_endpoint_readable_by_any_key_and_reconciles(monkeypatch, tmp_path):
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_ROOTS", "")
    get_settings.cache_clear()
    c = TestClient(create_app())

    # 배포된 앱에 심어 주는 키는 admin이 아니다 — 그 키로도 현황은 보여야 한다.
    member = c.post(f"{API}/keys", json={"name": "app"}, headers=ADMIN).json()["key"]
    body = c.get(f"{API}/scheduler", headers={"x-api-key": member})
    assert body.status_code == 200, body.text
    # 목록은 사람이 등록하지 않는다 — 처음 열어도 이미 채워져 있다.
    assert {j["name"] for j in body.json()["jobs"]} == {"api_catalog", "doc_index:internal"}

    job_id = body.json()["jobs"][0]["id"]
    assert c.post(f"{API}/scheduler/jobs/{job_id}/toggle",
                  headers={"x-api-key": member}).status_code == 403
    assert c.post(f"{API}/scheduler/jobs/{job_id}/run",
                  headers={"x-api-key": member}).status_code == 403
    get_settings.cache_clear()


def test_toggle_and_run_are_audited(monkeypatch, tmp_path):
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_ROOTS", "")
    get_settings.cache_clear()
    c = TestClient(create_app())

    jobs = c.get(f"{API}/scheduler", headers=ADMIN).json()["jobs"]
    job = next(j for j in jobs if j["name"] == "doc_index:internal")

    off = c.post(f"{API}/scheduler/jobs/{job['id']}/toggle", headers=ADMIN)
    assert off.json() == {"id": job["id"], "name": "doc_index:internal", "enabled": False}
    assert c.post(f"{API}/scheduler/jobs/{job['id']}/toggle",
                  headers=ADMIN).json()["enabled"] is True

    # 실패해도 200이다 — 결과가 곧 답이고, 그것을 모니터가 읽는다.
    ran = c.post(f"{API}/scheduler/jobs/{job['id']}/run", headers=ADMIN)
    assert ran.status_code == 200, ran.text
    assert ran.json()["job"] == "doc_index:internal"

    assert c.post(f"{API}/scheduler/jobs/99999/run", headers=ADMIN).status_code == 404

    actions = [e["action"] for e in c.get(f"{API}/audit", headers=ADMIN).json()]
    assert "scheduler.toggle" in actions
    assert "scheduler.run" in actions
    get_settings.cache_clear()
