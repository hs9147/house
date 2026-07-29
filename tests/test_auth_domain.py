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
