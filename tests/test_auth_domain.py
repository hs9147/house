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
