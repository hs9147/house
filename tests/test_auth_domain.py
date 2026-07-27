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
    assert data["key"].startswith("paas_")

    # 3. 중복 이메일 가입 시 400 차단
    res_dup = client.post(
        "/paas/api/v1/auth/register",
        json={"email": "newuser@cho-fam.com", "name": "홍길동", "password": "pass"},
    )
    assert res_dup.status_code == 400
