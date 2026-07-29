import hashlib
from datetime import timedelta

import pytest
from app.config import get_settings
from app.security import validate_email_domain


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
    # 응답의 key는 비밀번호가 아니라 난수 세션 토큰이다
    assert data["key"] != "pass"
    assert data["key"].startswith("paass_")

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
    user_key = res_reg.json()["key"]

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
    assert client.post("/paas/api/v1/auth/login",
                       json={"email": "victim@cho-fam.com", "password": "pw-correct"}).status_code == 200


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

    token = client.post("/paas/api/v1/auth/register",
                        json={"email": "u@cho-fam.com", "name": "user1", "password": "pw-1234"}).json()["key"]
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

    token = client.post("/paas/api/v1/auth/register",
                        json={"email": "u2@cho-fam.com", "name": "user2", "password": "pw-1234"}).json()["key"]
    assert client.post("/paas/api/v1/auth/logout", headers={"x-api-key": token}).status_code == 204
    assert client.get("/paas/api/v1/auth/me", headers={"x-api-key": token}).status_code == 401
