"""갭5 — OpenBao에서 Fernet 키 로드. 실패 시 침묵 폴백 없이 명확한 에러."""
import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app import security

VALID_KEY = Fernet.generate_key().decode()


class _Res:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def bao_env(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_OPENBAO_URL", "https://bao.test")
    monkeypatch.setenv("PAAS_OPENBAO_TOKEN", "s.token")
    get_settings.cache_clear()
    monkeypatch.setattr(security, "_fernet", None)
    yield monkeypatch
    monkeypatch.setattr(security, "_fernet", None)


def test_loads_key_from_openbao(bao_env):
    seen = {}

    def fake_get(url, headers, timeout):
        seen["url"] = url
        seen["token"] = headers["X-Vault-Token"]
        return _Res(200, {"data": {"data": {"key": VALID_KEY}}})

    import httpx

    bao_env.setattr(httpx, "get", fake_get)
    f = security.get_fernet()
    assert f.decrypt(f.encrypt(b"secret")) == b"secret"
    assert seen["url"] == "https://bao.test/v1/secret/data/paas/fernet"
    assert seen["token"] == "s.token"


def test_http_error_raises_clearly(bao_env):
    import httpx

    bao_env.setattr(httpx, "get", lambda url, headers, timeout: _Res(403, {}))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        security.get_fernet()


def test_missing_key_field_raises(bao_env):
    import httpx

    bao_env.setattr(httpx, "get", lambda url, headers, timeout: _Res(200, {"data": {"data": {}}}))
    with pytest.raises(RuntimeError, match="data.data.key"):
        security.get_fernet()


def test_env_key_used_when_openbao_unset(monkeypatch, fresh_settings):
    monkeypatch.delenv("PAAS_OPENBAO_URL", raising=False)
    monkeypatch.setenv("PAAS_FERNET_KEY", VALID_KEY)
    get_settings.cache_clear()
    monkeypatch.setattr(security, "_fernet", None)
    f = security.get_fernet()
    assert f.decrypt(f.encrypt(b"x")) == b"x"
    monkeypatch.setattr(security, "_fernet", None)
