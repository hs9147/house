"""잘못된 PAAS_ 설정값이 났을 때의 오류 표현.

값 하나만 틀려도 Settings() 생성이 실패해 **플랫폼 전체가 기동하지 못한다**(콘솔
로그인부터 모든 API까지). 이때 pydantic 기본 오류는 내부 필드명과 트레이스백이라,
서비스로 띄운 환경에서는 로그를 봐도 원인을 짚기 어렵고 프록시 쪽에서는 전 경로 502로만
보인다 — 그래서 어떤 환경변수가 왜 틀렸는지로 바꿔서 알려준다.
"""
import pytest

from app.config import SettingsError, get_settings


def test_invalid_boolean_names_the_env_var_and_value(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "truee")
    get_settings.cache_clear()
    with pytest.raises(SettingsError) as exc:
        get_settings()
    msg = str(exc.value)
    assert "PAAS_OIDC_PROVIDER_ENABLED" in msg  # pydantic 내부 필드명이 아니라 환경변수 이름
    assert "truee" in msg  # 무슨 값이 들어가 있는지


def test_all_bad_values_are_listed_at_once(monkeypatch, fresh_settings):
    """하나 고치고 다시 띄우고를 반복하지 않도록 잘못된 값을 한 번에 다 보여준다."""
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "truee")
    monkeypatch.setenv("PAAS_DEPLOY_WORKERS", "two")
    get_settings.cache_clear()
    with pytest.raises(SettingsError) as exc:
        get_settings()
    msg = str(exc.value)
    assert "PAAS_OIDC_PROVIDER_ENABLED" in msg
    assert "PAAS_DEPLOY_WORKERS" in msg


def test_valid_settings_still_load(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "true")
    get_settings.cache_clear()
    assert get_settings().oidc_provider_enabled is True
