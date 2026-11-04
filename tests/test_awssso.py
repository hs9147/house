"""AWS SSO 세션 유지(services/awssso) — 서버가 토큰을 스스로 갱신하고, 세션 시간을 간격을 바꿔 가며 배운다.

가짜 AWS는 botocore와 같은 규칙으로 움직인다: 액세스 토큰은 1시간짜리, 갱신은 만료 15분 안에서만 되고, 세션
시간(Identity Center 설정 — 서버는 모른다)이 지나면 갱신이 거절된다. 갱신이 거절돼도 토큰이 살아 있는 동안은
조용히 넘어가고, 토큰이 만료되면 그때 SsoExpired다.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import awssso, bedrock

T0 = datetime(2026, 10, 30, 0, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


class FakeAws:
    def __init__(self, session: timedelta, refreshable: bool = True):
        self.session = session
        self.refreshable = refreshable
        self.now = T0
        self.refreshes = 0
        self.login(T0)

    def login(self, at: datetime):
        self.login_at = at
        self.expires = at + HOUR
        self.written = at

    def state(self, _profile):
        return {"expires_at": self.expires.isoformat(), "refreshable": self.refreshable,
                "registration_expires_at": "", "written_at": self.written.isoformat()}

    def refresh(self, _profile):
        if self.expires - self.now < timedelta(minutes=15) and self.now < self.login_at + self.session:
            self.expires, self.written = self.now + HOUR, self.now
            self.refreshes += 1
        if self.expires <= self.now:
            raise bedrock.SsoExpired("p")

    def frozen(self, _profile):
        if self.expires <= self.now:
            raise bedrock.SsoExpired("p")


@pytest.fixture
def aws(monkeypatch):
    fake = FakeAws(session=timedelta(hours=8))
    monkeypatch.setattr(bedrock, "botocore_available", lambda: True)
    monkeypatch.setattr(bedrock, "sso_token_state", fake.state)
    monkeypatch.setattr(bedrock, "refresh_sso_token", fake.refresh)
    monkeypatch.setattr(bedrock, "_frozen", fake.frozen)
    return fake


def _run(aws: FakeAws, until: datetime, relogin_after=timedelta(minutes=7), detail=None):
    """서버처럼 돈다 — keep_alive가 정한 간격대로. 세션이 끝나면 사람이 잠시 뒤 다시 로그인한다."""
    outages = []
    while aws.now < until:
        status, detail, seconds = awssso.keep_alive("p", detail, now=aws.now)
        if detail.get("ended_at") and status == "failed" and aws.expires <= aws.now:
            outages.append(aws.now)
            aws.now += relogin_after
            aws.login(aws.now)
            continue
        aws.now += timedelta(seconds=seconds)
    return detail, outages


def test_server_keeps_the_token_alive_for_the_whole_session(aws):
    """사람의 요청이 없어도 서버가 창 안에서 갱신한다 — 세션이 끝날 때까지 토큰이 끊기지 않는다."""
    detail = None
    while aws.now < T0 + timedelta(hours=7):
        _status, detail, seconds = awssso.keep_alive("p", detail, now=aws.now)
        assert aws.expires > aws.now, f"토큰이 {aws.now}에 끊겼다"
        assert seconds <= 3600
        aws.now += timedelta(seconds=seconds)
    assert aws.refreshes >= 7          # 한 시간에 한 번은 갱신했다


@pytest.mark.parametrize("hours", [4, 8, 12, 23])
def test_session_length_is_learned_by_steering_the_refresh_time(aws, hours):
    """처음 본 세션은 로그인 시각을 모르니 배우지 않는다. 그 뒤로는 세션마다 구간이 좁아지고 늘 진짜 값을 품는다."""
    aws.session = timedelta(hours=hours)
    detail, outages = _run(aws, T0 + timedelta(days=10))
    low, high = detail["session_seconds"]
    assert low <= hours * 3600 <= high
    assert high - low <= awssso.GOOD_ENOUGH.total_seconds(), (low, high)
    # 좁혀 둔 것은 오래된 세션을 접어도 남는다(세션 기록은 늘 KEEP_SESSIONS개 이하)
    assert len(detail["sessions"]) <= awssso.KEEP_SESSIONS
    assert len(outages) > awssso.KEEP_SESSIONS or hours == 23


@pytest.mark.parametrize("hours, width_minutes", [(1.5, 35), (2, 24), (3, 13)])
def test_short_sessions_narrow_only_to_the_gap_aws_leaves(aws, hours, width_minutes):
    """갱신 한 번에 46~57분씩만 나아가므로 로그인 직후 몇 시간은 시도할 수 없는 틈이 있다 — 그만큼만 좁혀진다."""
    aws.session = timedelta(hours=hours)
    detail, _ = _run(aws, T0 + timedelta(days=10))
    low, high = detail["session_seconds"]
    assert low <= hours * 3600 <= high
    assert (high - low) / 60 <= width_minutes + 0.5


def test_without_steering_the_interval_stays_wide(aws, monkeypatch):
    """대조군 — 몰아 가지 않으면 갱신이 로그인에서 늘 같은 자리에 떨어져 구간이 좁혀지지 않는다."""
    monkeypatch.setattr(awssso, "next_check",
                        lambda now, expires, *a, **k: (max(expires - awssso.EARLY, now), "refresh"))
    detail, _ = _run(aws, T0 + timedelta(days=6))
    low, high = detail["session_seconds"]
    assert high - low > 30 * 60


def test_ended_session_is_reported_and_checked_often_until_someone_logs_in(aws):
    detail = None
    aws.now = T0 + timedelta(hours=9)          # 세션(8시간)도 토큰도 끝났다
    status, detail, seconds = awssso.keep_alive("p", detail, now=aws.now)
    assert status == "failed" and "다시 로그인" in detail["reason"]
    assert seconds == awssso.ENDED_POLL.total_seconds()
    aws.login(aws.now + timedelta(minutes=3))
    aws.now += timedelta(minutes=5)
    status, detail, _ = awssso.keep_alive("p", detail, now=aws.now)
    assert status == "ok" and detail["ended_at"] is None
    assert detail["login_at"] == aws.login_at.isoformat()   # 다시 로그인한 때(캐시가 쓰인 때)부터 센다
    assert "reason" not in detail


def test_refresh_failure_inside_the_window_is_retried_before_calling_it_an_end(aws):
    """창 안에서 한 번 갱신이 안 된 것은 일시적일 수 있다 — 토큰이 살아 있는 동안 다시 본다."""
    aws.session = timedelta(minutes=50)        # 첫 창(45분~)에서 이미 거절된다
    aws.now = T0 + timedelta(minutes=50)
    status, detail, seconds = awssso.keep_alive("p", {"login_at": None}, now=aws.now)
    assert status == "failed" and detail["first_fail_at"]
    assert seconds <= awssso.RETRY.total_seconds()


def test_next_check_probes_the_middle_of_the_estimate():
    login = T0
    est = (7 * 3600.0, 8 * 3600.0)                 # 7시간~8시간 사이에 끝난다고 배웠다
    probe = login + timedelta(hours=7, minutes=30)
    when, why = awssso.next_check(probe - timedelta(minutes=5), probe + timedelta(minutes=8), login, est)
    assert (when, why) == (probe, "probe")          # 창 안에 가운데가 있으면 그때 시도한다
    when, why = awssso.next_check(T0 + HOUR, T0 + 2 * HOUR, login, est)
    assert why == "steer" and T0 + 2 * HOUR - awssso.EARLY <= when <= T0 + 2 * HOUR - awssso.LATE
    when, why = awssso.next_check(T0, T0 + HOUR, None, None)
    assert (when, why) == (T0 + HOUR - awssso.EARLY, "refresh")   # 배울 것이 없으면 창이 열리자마자


def test_estimate_trusts_the_latest_session_when_the_setting_changed():
    assert awssso.estimate([]) is None
    assert awssso.estimate([{"lower": 100, "upper": 300}, {"lower": 200, "upper": 400}]) == (200, 300)
    # 세션 시간을 줄였다 — 앞의 구간과 겹치지 않으면 마지막 것만 믿는다
    assert awssso.estimate([{"lower": 7000, "upper": 8000}, {"lower": 3000, "upper": 3600}]) == (3000, 3600)


def test_without_botocore_the_job_is_skipped_not_failed(monkeypatch):
    monkeypatch.setattr(bedrock, "botocore_available", lambda: False)
    status, detail, seconds = awssso.keep_alive("p", None, now=T0)
    assert status == "skipped" and "botocore" in detail["reason"] and seconds == 3600


def test_scheduler_makes_one_job_per_bedrock_profile(fresh_settings):
    from app.db import SessionLocal
    from app.main import create_app
    from app.models import JobKind, LlmProvider, LlmProviderKind, ScheduledJob
    from app.services import scheduler

    create_app()
    db = SessionLocal()
    try:
        db.add_all([
            LlmProvider(name="b1", kind=LlmProviderKind.aws, base_url="x", model="m", aws_profile="dev"),
            LlmProvider(name="b2", kind=LlmProviderKind.aws, base_url="x", model="m2", aws_profile="dev"),
            LlmProvider(name="gw", kind=LlmProviderKind.aws, base_url="x", model="m"),   # 프로필 없는 게이트웨이
            LlmProvider(name="oa", kind=LlmProviderKind.openai, base_url="x", model="m"),
        ])
        db.commit()
        scheduler.reconcile(db)
        jobs = [j.name for j in db.query(ScheduledJob).filter(ScheduledJob.kind == JobKind.aws_sso)]
        assert jobs == ["aws_sso:dev"]
    finally:
        db.close()


def test_ended_session_starts_a_login_on_the_server_and_retries_with_backoff(aws):
    """이 job이 있는 까닭 — 세션이 끝나면 서버에서 다시 로그인을 시작한다(승인 주소는 사람에게). 주소가 살아 있는
    동안은 새로 받지 않고, 승인이 없으면 30분·1시간·2시간·4시간 간격으로 새 주소를 받는다. 승인되면 지운다."""
    started = []

    def login(profile):
        started.append(aws.now)
        return {"verification_url": f"https://device.sso/?code={len(started)}", "user_code": "ABCD-EFGH",
                "code_autofilled": True}

    aws.now = T0 + timedelta(hours=9)
    status, detail, _ = awssso.keep_alive("p", None, now=aws.now, login=login)
    assert status == "failed" and detail.pop("login_new") is True
    assert detail["login"]["url"].endswith("code=1") and detail["login"]["attempt"] == 1
    assert "다시 로그인을 시작했습니다" in detail["reason"]
    for minutes in (5, 10, 25):                       # 30분 전에는 새로 받지 않는다(메일이 쏟아지지 않게)
        aws.now = T0 + timedelta(hours=9, minutes=minutes)
        status, detail, _ = awssso.keep_alive("p", detail, now=aws.now, login=login)
        assert "login_new" not in detail
    assert len(started) == 1
    for at, attempt in ((timedelta(hours=9, minutes=31), 2), (timedelta(hours=10, minutes=32), 3),
                        (timedelta(hours=12, minutes=33), 4), (timedelta(hours=16, minutes=34), 5)):
        aws.now = T0 + at - timedelta(minutes=2)       # 아직 간격 전
        _s, detail, _ = awssso.keep_alive("p", detail, now=aws.now, login=login)
        assert detail["login"]["attempt"] == attempt - 1
        aws.now = T0 + at
        _s, detail, _ = awssso.keep_alive("p", detail, now=aws.now, login=login)
        assert detail.pop("login_new") is True and detail["login"]["attempt"] == attempt
    aws.login(aws.now + timedelta(minutes=2))          # 누군가 승인했다
    aws.now += timedelta(minutes=5)
    status, detail, _ = awssso.keep_alive("p", detail, now=aws.now, login=login)
    assert status == "ok" and "login" not in detail and detail["login_at"] == aws.login_at.isoformat()


def test_a_login_that_cannot_start_says_why(aws):
    def login(_profile):
        raise bedrock.BedrockError("서버에 aws CLI가 없어 SSO 로그인을 시작할 수 없습니다")

    aws.now = T0 + timedelta(hours=9)
    status, detail, _ = awssso.keep_alive("p", None, now=aws.now, login=login)
    assert status == "failed" and "login_new" not in detail
    assert "로그인을 시작하지 못했습니다" in detail["reason"] and "aws CLI" in detail["reason"]


def test_scheduler_mails_the_approval_link_to_admins(fresh_settings, monkeypatch):
    """서버가 다시 로그인을 시작하면 승인 주소를 관리자 메일로(noreply) — 콘솔을 열지 않아도 안다."""
    from app.db import SessionLocal
    from app.main import create_app
    from app.models import JobKind, ScheduledJob, UserAccount
    from app.services import mailer, scheduler

    monkeypatch.setenv("PAAS_SMTP_HOST", "lgesmtp.lge.com")
    monkeypatch.setenv("PAAS_MAIL_FROM", "GPAX <gpax-noreply@lge.com>")
    monkeypatch.setenv("PAAS_MAIL_ALLOWED_DOMAINS", "lge.com")
    from app.config import get_settings
    get_settings.cache_clear()
    create_app()
    sent = []
    monkeypatch.setattr(mailer, "send", lambda actor, to, subject, body, **kw: sent.append((actor, to, subject, body)))
    monkeypatch.setattr(awssso, "keep_alive", lambda profile, prev, login=None: (
        "failed", {"reason": "x", "login": {"url": "https://device.sso/?code=9", "attempt": 1, "started_at": "t"},
                   "login_new": True}, 300))
    db = SessionLocal()
    try:
        db.query(UserAccount).delete()
        db.add_all([UserAccount(email="admin@lge.com", password_hash="x", is_admin=True),
                    UserAccount(email="user@lge.com", password_hash="x", is_admin=False),
                    UserAccount(email="admin@gmail.com", password_hash="x", is_admin=True)])
        job = ScheduledJob(name="aws_sso:p", kind=JobKind.aws_sso, target="p", interval_seconds=300)
        db.add(job)
        db.commit()
        scheduler.run(db, job)
        assert [s[1] for s in sent] == [["admin@lge.com"]]          # 사내 주소인 관리자만
        assert "https://device.sso/?code=9" in sent[0][3] and "AWS SSO" in sent[0][2]
        assert "login_new" not in job.last_detail and "1명" in job.last_detail["login"]["notified"]
    finally:
        db.rollback()
        db.query(ScheduledJob).delete()
        db.query(UserAccount).delete()
        db.commit()
        db.close()
