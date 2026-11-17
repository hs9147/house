"""임시 작업(services/tempjobs) — 코드에 둔 운영 스크립트: 사용자 등록 즉시 승인, 소속 없는 계정에 조직 gp3(따로 켜고 끈다)."""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import AuditEvent, JobKind, Organization, ScheduledJob, UserAccount
from app.services import scheduler, tempjobs

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def _client(monkeypatch):
    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "lge.com")
    get_settings.cache_clear()
    c = TestClient(create_app())
    with SessionLocal() as db:
        scheduler.reconcile(db)                     # 스케줄러에 임시 작업이 올라간다(켜짐)
    return c


def _register(c, email):
    return c.post(f"{API}/auth/register", json={"email": email, "name": "새 사용자", "password": "pw-1234"})


def test_registration_is_approved_at_once_and_joins_gp3(monkeypatch, fresh_settings):
    c = _client(monkeypatch)
    with SessionLocal() as db:
        db.add(Organization(name="gp3"))
        db.commit()
    res = _register(c, "new@lge.com")
    assert res.status_code == 201 and res.json()["is_approved"] is True
    assert c.post(f"{API}/auth/login", json={"email": "new@lge.com", "password": "pw-1234"}).status_code == 200
    with SessionLocal() as db:
        user = db.query(UserAccount).filter_by(email="new@lge.com").one()
        assert [o.name for o in user.organizations] == ["gp3"] and user.organization.name == "gp3"
        event = db.query(AuditEvent).filter(AuditEvent.action == "tempjob.auto_approve").one()
        assert event.actor == "event:user.register" and event.detail["approved"] == ["new@lge.com"]
        joined = db.query(AuditEvent).filter(AuditEvent.action == "tempjob.auto_org").one()
        assert joined.detail["joined"] == ["new@lge.com"]


def test_waiting_accounts_are_swept_and_a_missing_org_is_reported(monkeypatch, fresh_settings):
    c = _client(monkeypatch)
    with SessionLocal() as db:
        db.add(UserAccount(email="old@lge.com", name="", password_hash="x", is_approved=False))
        db.commit()
        job = tempjobs.job_of(db, "auto_approve")
        result = scheduler.run(db, job)                # 주기 실행
    assert result["status"] == "ok" and result["detail"]["approved"] == ["old@lge.com"]
    with SessionLocal() as db:
        org_run = scheduler.run(db, tempjobs.job_of(db, "auto_org"))   # 조직 gp3이 없다 — 넣지 못했다고만
    assert org_run["status"] == "skipped" and "gp3이 없어" in org_run["detail"]["note"]
    with SessionLocal() as db:
        assert db.query(UserAccount).filter_by(email="old@lge.com").one().is_approved
        assert scheduler.run(db, tempjobs.job_of(db, "auto_approve"))["status"] == "skipped"   # 할 일 없음
        assert db.query(AuditEvent).filter(AuditEvent.action == "tempjob.auto_approve").count() == 1   # 이력은 한 번


def test_turning_it_off_keeps_the_approval_gate(monkeypatch, fresh_settings):
    c = _client(monkeypatch)
    with SessionLocal() as db:
        job = tempjobs.job_of(db, "auto_approve")
        job.enabled = False
        db.commit()
    assert _register(c, "wait@lge.com").json()["is_approved"] is False


def test_dashboard_lists_runs_and_toggles_temp_jobs(monkeypatch, fresh_settings):
    c = _client(monkeypatch)
    assert c.get(f"{API}/temp-jobs").status_code == 401
    tasks = c.get(f"{API}/temp-jobs", headers=ADMIN).json()["tasks"]
    task = next(t for t in tasks if t["id"] == "auto_approve")
    assert task["job"]["enabled"] is True and task["events"] == ["user.register"] and "gp3" in task["description"]
    with SessionLocal() as db:
        db.add(UserAccount(email="later@lge.com", name="", password_hash="x", is_approved=False))
        db.commit()
    ran = c.post(f"{API}/temp-jobs/auto_approve/run", headers=ADMIN).json()
    assert ran["status"] == "ok" and ran["detail"]["approved"] == ["later@lge.com"]
    task = next(t for t in c.get(f"{API}/temp-jobs", headers=ADMIN).json()["tasks"] if t["id"] == "auto_approve")
    assert task["history"][0]["detail"]["approved"] == ["later@lge.com"] and task["job"]["last_status"] == "ok"
    assert c.post(f"{API}/temp-jobs/nope/run", headers=ADMIN).status_code == 404
    with SessionLocal() as db:
        assert db.query(ScheduledJob).filter(ScheduledJob.kind == JobKind.temp_job).count() == len(tempjobs.TASKS)


def test_the_two_jobs_are_separate(monkeypatch, fresh_settings):
    """승인과 부서 넣기는 따로 켜고 끈다 — 승인을 끄면 부서만 들어가고, 부서 넣기를 끄면 승인만 된다.
    이미 다른 부서가 있는 계정에는 gp3을 더하지 않는다."""
    c = _client(monkeypatch)
    with SessionLocal() as db:
        db.add_all([Organization(name="gp3"), Organization(name="sales")])
        db.commit()
        tempjobs.job_of(db, "auto_approve").enabled = False
        db.commit()
    assert _register(c, "only-org@lge.com").json()["is_approved"] is False
    with SessionLocal() as db:
        user = db.query(UserAccount).filter_by(email="only-org@lge.com").one()
        assert [o.name for o in user.organizations] == ["gp3"]
        tempjobs.job_of(db, "auto_approve").enabled = True
        tempjobs.job_of(db, "auto_org").enabled = False
        db.commit()
    assert _register(c, "only-approve@lge.com").json()["is_approved"] is True
    with SessionLocal() as db:
        assert db.query(UserAccount).filter_by(email="only-approve@lge.com").one().organizations == []
        sales = db.query(Organization).filter_by(name="sales").one()
        other = UserAccount(email="sales@lge.com", name="", password_hash="x", is_approved=True)
        other.organizations.append(sales)
        db.add(other)
        db.commit()
        tempjobs.job_of(db, "auto_org").enabled = True
        db.commit()
        result = scheduler.run(db, tempjobs.job_of(db, "auto_org"))
    assert result["detail"]["joined"] == ["only-approve@lge.com"]           # 다른 부서가 있는 sales@는 그대로
    with SessionLocal() as db:
        assert [o.name for o in db.query(UserAccount).filter_by(email="sales@lge.com").one().organizations] == ["sales"]
