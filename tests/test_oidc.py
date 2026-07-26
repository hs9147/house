"""OIDC/RBAC 베어러 인증 — RS256 자체 서명 토큰으로 검증 (JWKS 조회는 monkeypatch)."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app import security

ISSUER = "https://sso.test/realms/company"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()


class _FakeSigningKey:
    key = _public_key


class _FakeJwkClient:
    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey()


def _token(roles: list[str], exp_offset: int = 3600, issuer: str = ISSUER) -> str:
    payload = {
        "iss": issuer,
        "sub": "user-1",
        "preferred_username": "hong",
        "exp": int(time.time()) + exp_offset,
        "realm_access": {"roles": roles},
    }
    return jwt.encode(payload, _private_key, algorithm="RS256")


@pytest.fixture
def oidc_client(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_OIDC_ISSUER", ISSUER)
    get_settings.cache_clear()
    monkeypatch.setattr(security, "_jwk_client", _FakeJwkClient())
    yield TestClient(create_app())
    monkeypatch.setattr(security, "_jwk_client", None)


def _auth(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


def test_member_role_can_list_but_not_audit(oidc_client):
    token = _token(["developer"])
    assert oidc_client.get("/paas/api/v1/projects", headers=_auth(token)).status_code == 200
    assert oidc_client.get("/paas/api/v1/audit", headers=_auth(token)).status_code == 403


def test_admin_role_grants_admin(oidc_client):
    token = _token(["developer", "paas-admin"])
    assert oidc_client.get("/paas/api/v1/audit", headers=_auth(token)).status_code == 200
    # 감사 로그에 OIDC 사용자명이 기록되는지 — 키 발급으로 확인
    r = oidc_client.post("/paas/api/v1/keys", json={"name": "from-oidc"}, headers=_auth(token))
    assert r.status_code == 201
    rows = oidc_client.get("/paas/api/v1/audit", headers=_auth(token)).json()
    assert any(a["actor"] == "hong" and a["action"] == "key.issue" for a in rows)


def test_expired_token_rejected(oidc_client):
    token = _token(["paas-admin"], exp_offset=-60)
    assert oidc_client.get("/paas/api/v1/projects", headers=_auth(token)).status_code == 401


def test_wrong_issuer_rejected(oidc_client):
    token = _token(["developer"], issuer="https://evil.test")
    assert oidc_client.get("/paas/api/v1/projects", headers=_auth(token)).status_code == 401


def test_bearer_rejected_when_oidc_not_configured(fresh_settings, monkeypatch):
    monkeypatch.delenv("PAAS_OIDC_ISSUER", raising=False)
    get_settings.cache_clear()
    c = TestClient(create_app())
    r = c.get("/paas/api/v1/projects", headers=_auth(_token(["developer"])))
    assert r.status_code == 401
    assert "OIDC not configured" in r.json()["detail"]


def test_api_key_still_works_alongside_oidc(oidc_client):
    assert oidc_client.get("/paas/api/v1/projects", headers={"x-api-key": "test-admin-key"}).status_code == 200
