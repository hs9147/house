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


def test_startup_logs_survive_a_cp949_console(monkeypatch, fresh_settings, tmp_path):
    """회귀: 기동 로그의 한글·em dash가 Windows 기본 인코딩(한국어판 cp949)에서
    UnicodeEncodeError를 내면 create_app이 예외로 끝난다. app = create_app()이 모듈
    최상단에서 실행되므로 uvicorn은 import 시점에 죽고, 콘솔 로그인부터 모든 API까지
    전 경로가 502가 된다 — 로그 한 줄 때문에 플랫폼 전체가 안 뜨는 셈이다.
    """
    import io
    import os

    from app.main import create_app

    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", "https://public.example.com")
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_BACKCHANNEL_URL", "http://localhost:7000/paas")
    # 두 값을 다르게 둬서 경고 줄(한글이 더 긴 쪽)까지 출력되게 한다
    monkeypatch.setenv("PAAS_OIDC_ISSUER", "https://public.example.com/paas")
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH", str(tmp_path / "k.pem"))
    get_settings.cache_clear()

    # 한국어판 Windows 콘솔을 흉내낸다 — errors="strict"가 실제 기본값이다.
    devnull = open(os.devnull, "wb")
    monkeypatch.setattr(
        "sys.stdout", io.TextIOWrapper(devnull, encoding="cp949", errors="strict"),
    )
    try:
        create_app()  # UnicodeEncodeError가 나면 안 된다
    finally:
        devnull.close()
