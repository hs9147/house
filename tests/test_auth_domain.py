import hashlib
from datetime import timedelta

import pytest
from app.config import get_settings
from app.security import validate_email_domain


ADMIN = {"x-api-key": "test-admin-key"}


def _approve_and_login(client, email, password):
    """가입 → 관리자 승인 → 로그인. 승인 도입 후 계정을 쓰려면 이 경로를 거쳐야 한다."""
    accounts = client.get("/paas/api/v1/auth/accounts", headers=ADMIN).json()
    account_id = next(a["id"] for a in accounts if a["email"] == email)
    client.post(f"/paas/api/v1/auth/accounts/{account_id}/approve", headers=ADMIN)
    return client.post("/paas/api/v1/auth/login",
                       json={"email": email, "password": password}).json()["key"]


def test_validate_email_domain_default(monkeypatch):
    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "cho-fam.com")
    get_settings.cache_clear()

    assert validate_email_domain("user@cho-fam.com") is True
    assert validate_email_domain("admin@sub.cho-fam.com") is True
    assert validate_email_domain("user@other-domain.com") is False
    assert validate_email_domain("invalid-email") is False


def test_validate_email_domain_custom(monkeypatch):
    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "company.co.kr")
    get_settings.cache_clear()

    assert validate_email_domain("employee@company.co.kr") is True
    assert validate_email_domain("employee@other.com") is False


def test_validate_email_domain_empty_allows_all(monkeypatch):
    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "")
    get_settings.cache_clear()

    assert validate_email_domain("user@anydomain.com") is True


def test_register_user_account_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app

    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "cho-fam.com")
    get_settings.cache_clear()
    client = TestClient(create_app())

    # 1. 허용 도메인이 아닌 이메일은 403 차단
    res = client.post(
        "/paas/api/v1/auth/register",
        json={"email": "user@other.com", "name": "홍길동", "password": "pass"},
    )
    assert res.status_code == 403

    # 2. 허용 도메인 계정 가입 성공
    res_ok = client.post(
        "/paas/api/v1/auth/register",
        json={"email": "newuser@cho-fam.com", "name": "홍길동", "password": "pass"},
    )
    assert res_ok.status_code == 201
    data = res_ok.json()
    assert data["email"] == "newuser@cho-fam.com"
    # 가입은 신청일 뿐 — 승인 전에는 세션(key)을 주지 않는다
    assert data["is_approved"] is False
    assert "key" not in data

    # 3. 중복 이메일 가입 시 400 차단
    res_dup = client.post(
        "/paas/api/v1/auth/register",
        json={"email": "newuser@cho-fam.com", "name": "홍길동", "password": "pass"},
    )
    assert res_dup.status_code == 400


def test_user_account_login_and_api_permissions(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app

    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "cho-fam.com")
    get_settings.cache_clear()
    client = TestClient(create_app())

    # 1. 회원가입
    res_reg = client.post(
        "/paas/api/v1/auth/register",
        json={"email": "testuser@cho-fam.com", "name": "테스트유저", "password": "mypassword123"},
    )
    assert res_reg.status_code == 201
    user_key = _approve_and_login(client, "testuser@cho-fam.com", "mypassword123")

    # 2. 로그인 API (/paas/api/v1/auth/login) 검증
    res_login = client.post(
        "/paas/api/v1/auth/login",
        json={"email": "testuser@cho-fam.com", "password": "mypassword123"},
    )
    assert res_login.status_code == 200
    assert res_login.json()["email"] == "testuser@cho-fam.com"

    # 3. /auth/me 접근 권한 검증
    res_me = client.get("/paas/api/v1/auth/me", headers={"x-api-key": user_key})
    assert res_me.status_code == 200
    assert res_me.json()["name"] == "testuser@cho-fam.com"

    # 4. /orgs 목록 조회 접근 권한 검증
    res_orgs = client.get("/paas/api/v1/orgs", headers={"x-api-key": user_key})
    assert res_orgs.status_code == 200
    assert isinstance(res_orgs.json(), list)

    # 5. /projects 목록 조회 및 생성 접근 권한 검증
    res_projects = client.get("/paas/api/v1/projects", headers={"x-api-key": user_key})
    assert res_projects.status_code == 200
    assert isinstance(res_projects.json(), list)



def test_stored_hash_is_not_accepted_as_a_password(monkeypatch):
    """pass-the-hash 차단 — DB에서 얻은 password_hash를 그대로 제출해 로그인할 수 없다."""
    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.main import create_app
    from app.models import UserAccount

    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "cho-fam.com")
    get_settings.cache_clear()
    client = TestClient(create_app())

    client.post("/paas/api/v1/auth/register",
                json={"email": "victim@cho-fam.com", "name": "victim", "password": "pw-correct"})

    with SessionLocal() as db:
        stored = db.query(UserAccount).filter(UserAccount.email == "victim@cho-fam.com").one().password_hash

    # 비밀번호 원문도, 그 sha256도 저장되지 않는다 — 솔트 + scrypt만 남는다
    assert stored.startswith("scrypt$")
    assert "pw-correct" not in stored
    assert hashlib.sha256(b"pw-correct").hexdigest() not in stored

    res = client.post("/paas/api/v1/auth/login",
                      json={"email": "victim@cho-fam.com", "password": stored})
    assert res.status_code == 400

    # 거절된 시도가 저장된 해시를 덮어쓰지 않는다 (원래 비밀번호가 계속 통해야 한다)
    with SessionLocal() as db:
        assert db.query(UserAccount).filter(
            UserAccount.email == "victim@cho-fam.com").one().password_hash == stored
    assert _approve_and_login(client, "victim@cho-fam.com", "pw-correct").startswith("paass_")


def test_same_password_yields_different_hashes(monkeypatch):
    """솔트가 있으므로 같은 비밀번호라도 저장값이 겹치지 않는다(레인보우 테이블 무력화)."""
    from app.security import hash_password, verify_password

    a, b = hash_password("same-password"), hash_password("same-password")
    assert a != b
    assert verify_password("same-password", a) and verify_password("same-password", b)
    assert not verify_password("wrong", a)


def test_session_token_expires(monkeypatch):
    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.main import create_app
    from app.models import UserSession, utcnow

    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "cho-fam.com")
    get_settings.cache_clear()
    client = TestClient(create_app())

    client.post("/paas/api/v1/auth/register",
                json={"email": "u@cho-fam.com", "name": "user1", "password": "pw-1234"})
    token = _approve_and_login(client, "u@cho-fam.com", "pw-1234")
    assert client.get("/paas/api/v1/auth/me", headers={"x-api-key": token}).status_code == 200

    with SessionLocal() as db:
        row = db.query(UserSession).one()
        row.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()

    assert client.get("/paas/api/v1/auth/me", headers={"x-api-key": token}).status_code == 401


def test_logout_revokes_the_session(monkeypatch):
    """브라우저 저장소만 비우면 토큰은 계속 유효하다 — 서버에서 폐기되어야 한다."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "cho-fam.com")
    get_settings.cache_clear()
    client = TestClient(create_app())

    client.post("/paas/api/v1/auth/register",
                json={"email": "u2@cho-fam.com", "name": "user2", "password": "pw-1234"})
    token = _approve_and_login(client, "u2@cho-fam.com", "pw-1234")
    assert client.post("/paas/api/v1/auth/logout", headers={"x-api-key": token}).status_code == 204
    assert client.get("/paas/api/v1/auth/me", headers={"x-api-key": token}).status_code == 401


def _register(client, email="pending@cho-fam.com", password="pw-1234"):
    return client.post("/paas/api/v1/auth/register",
                       json={"email": email, "name": "가입자", "password": password})


def test_unapproved_account_cannot_log_in(monkeypatch):
    """도메인만 맞으면 누구나 들어오던 것을 막는다 — 승인 전에는 로그인 불가."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "cho-fam.com")
    get_settings.cache_clear()
    client = TestClient(create_app())

    assert _register(client).status_code == 201
    res = client.post("/paas/api/v1/auth/login",
                      json={"email": "pending@cho-fam.com", "password": "pw-1234"})
    assert res.status_code == 403
    assert "승인" in res.text

    # 비밀번호가 틀리면 승인 여부를 알려주지 않고 동일한 400을 준다
    wrong = client.post("/paas/api/v1/auth/login",
                        json={"email": "pending@cho-fam.com", "password": "nope-1234"})
    assert wrong.status_code == 400


def test_admin_approves_then_login_works(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "cho-fam.com")
    get_settings.cache_clear()
    client = TestClient(create_app())
    admin = ADMIN

    _register(client)
    accounts = client.get("/paas/api/v1/auth/accounts", headers=admin).json()
    assert [a["email"] for a in accounts] == ["pending@cho-fam.com"]
    assert accounts[0]["is_approved"] is False

    approved = client.post(f"/paas/api/v1/auth/accounts/{accounts[0]['id']}/approve", headers=admin)
    assert approved.status_code == 200
    assert approved.json()["is_approved"] is True

    ok = client.post("/paas/api/v1/auth/login",
                     json={"email": "pending@cho-fam.com", "password": "pw-1234"})
    assert ok.status_code == 200
    assert ok.json()["key"].startswith("paass_")


def test_reject_removes_account_and_revokes_sessions(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "cho-fam.com")
    get_settings.cache_clear()
    client = TestClient(create_app())
    admin = ADMIN

    _register(client)
    account_id = client.get("/paas/api/v1/auth/accounts", headers=admin).json()[0]["id"]
    client.post(f"/paas/api/v1/auth/accounts/{account_id}/approve", headers=admin)
    token = client.post("/paas/api/v1/auth/login",
                        json={"email": "pending@cho-fam.com", "password": "pw-1234"}).json()["key"]
    assert client.get("/paas/api/v1/auth/me", headers={"x-api-key": token}).status_code == 200

    assert client.delete(f"/paas/api/v1/auth/accounts/{account_id}", headers=admin).status_code == 204
    # 계정이 사라지면 이미 발급된 세션도 함께 죽는다
    assert client.get("/paas/api/v1/auth/me", headers={"x-api-key": token}).status_code == 401
    assert client.get("/paas/api/v1/auth/accounts", headers=admin).json() == []


def test_approval_endpoints_are_admin_only(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "cho-fam.com")
    get_settings.cache_clear()
    client = TestClient(create_app())
    admin = ADMIN

    _register(client)
    account_id = client.get("/paas/api/v1/auth/accounts", headers=admin).json()[0]["id"]
    client.post(f"/paas/api/v1/auth/accounts/{account_id}/approve", headers=admin)
    member = {"x-api-key": client.post(
        "/paas/api/v1/auth/login",
        json={"email": "pending@cho-fam.com", "password": "pw-1234"}).json()["key"]}

    assert client.get("/paas/api/v1/auth/accounts", headers=member).status_code == 403
    assert client.post(f"/paas/api/v1/auth/accounts/{account_id}/approve",
                       headers=member).status_code == 403
    assert client.delete(f"/paas/api/v1/auth/accounts/{account_id}",
                         headers=member).status_code == 403


def test_registration_request_is_mailed_to_admins(monkeypatch):
    """가입 요청이 들어오면 승인된 관리자 계정과 PAAS_REGISTER_NOTIFY_TO에 메일로 알린다(메일 발송이 설정돼 있을 때).
    본인에게는 보내지 않고, 승인 대기인지 자동 승인됐는지를 적는다. 메일 설정이 없으면 보내지 않는다."""
    import uuid

    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.main import create_app
    from app.models import AuditEvent, UserAccount
    from app.security import hash_password
    from app.services import mailer

    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "lge.com")
    monkeypatch.setenv("PAAS_REGISTER_NOTIFY_TO", "ops@lge.com; boss@lge.com")
    get_settings.cache_clear()
    tag = uuid.uuid4().hex[:6]
    c = TestClient(create_app())                     # 표를 먼저 만든다
    with SessionLocal() as db:
        db.add(UserAccount(email=f"admin-{tag}@lge.com", name="관리자", password_hash=hash_password("x"),
                           is_approved=True, is_admin=True))
        db.commit()
    sent: list[dict] = []
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send", lambda actor, to, subject, body, **kw: (
        sent.append({"to": to, "subject": subject, "body": body}) or {"recipients": len(to)}))
    from app.services import tempjobs
    monkeypatch.setattr(tempjobs, "TASKS", {})       # 자동 승인 없이 — 승인 대기

    email = f"new-{tag}@lge.com"
    r = c.post("/paas/api/v1/auth/register", json={"email": email, "name": "새 사람", "password": "pw-1234"})
    assert r.status_code == 201
    [mail] = sent
    assert f"admin-{tag}@lge.com" in mail["to"] and "ops@lge.com" in mail["to"] and "boss@lge.com" in mail["to"]
    assert email not in mail["to"] and "새 사람" in mail["subject"]
    assert email in mail["body"] and "승인 대기" in mail["body"] and "KST" in mail["body"]
    with SessionLocal() as db:
        event = db.query(AuditEvent).filter(AuditEvent.action == "user.register.notify",
                                            AuditEvent.target == email).one()
        assert event.detail["sent"] is True

    monkeypatch.setattr(mailer, "configured", lambda: False)
    sent.clear()
    c.post("/paas/api/v1/auth/register", json={"email": f"other-{tag}@lge.com", "name": "x", "password": "pw-1234"})
    assert sent == []


def test_admin_info_lists_admins_and_mail_settings(monkeypatch):
    """대시보드 「관리자 정보」 — 관리자 계정·관리자 키 이름과 메일 설정(가입 알림 수신자 포함). 관리자만 본다."""
    import uuid

    from fastapi.testclient import TestClient

    from app.db import SessionLocal
    from app.main import create_app
    from app.models import UserAccount
    from app.security import hash_password

    monkeypatch.setenv("PAAS_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("PAAS_MAIL_FROM", "GPAX <noreply@lge.com>")
    monkeypatch.setenv("PAAS_REGISTER_NOTIFY_TO", "ops@lge.com")
    get_settings.cache_clear()
    c = TestClient(create_app())
    tag = uuid.uuid4().hex[:6]
    with SessionLocal() as db:
        db.add(UserAccount(email=f"boss-{tag}@lge.com", name="대장", password_hash=hash_password("x"),
                           is_approved=True, is_admin=True))
        db.commit()
    r = c.get("/paas/api/v1/system/admin-info", headers=ADMIN)
    assert r.status_code == 200
    info = r.json()
    assert any(a["email"] == f"boss-{tag}@lge.com" and a["name"] == "대장" for a in info["admins"])
    assert all(set(k) == {"name", "created_at"} for k in info["admin_keys"])   # 키 값·해시는 내보내지 않는다
    mail = info["mail"]
    assert mail["configured"] is True and mail["smtp_host"] == "smtp.example.com"
    assert f"boss-{tag}@lge.com" in mail["register_notify_to"] and "ops@lge.com" in mail["register_notify_to"]
