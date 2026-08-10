"""paas 자체 OIDC Provider(Keycloak 없이 직접 구현) — Authorization Code 플로우 전체와,
발급한 토큰이 기존 authenticate_bearer(Keycloak용으로 짜여진 검증 코드)에 그대로
먹히는지까지 확인한다(핵심 요구사항: oidc_issuer를 paas 자신으로 돌려도 코드 변경 없음)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import security
from app.config import get_settings
from app.main import create_app
from app.models import UserAccount
from app.security import hash_password
from app.services import oidc_provider

CLIENT_ID = "gitea"
CLIENT_SECRET = "gitea-secret"
REDIRECT_URI = "https://git.example.com/user/oauth2/keycloak/callback"


@pytest.fixture(autouse=True)
def _reset_provider_singletons():
    """서명 키/kid는 모듈 전역 캐시라 테스트마다 다른 signing_key_path를 써도 이전
    테스트의 키가 계속 재사용되면 안 된다."""
    oidc_provider._signing_key = None
    oidc_provider._kid = None
    yield
    oidc_provider._signing_key = None
    oidc_provider._kid = None


@pytest.fixture
def provider_client(monkeypatch, fresh_settings, tmp_path):
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", "http://paas.test")
    monkeypatch.setenv(
        "PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH", str(tmp_path / "signing-key.pem"),
    )
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_CLIENTS", json.dumps({
        CLIENT_ID: {"secret": CLIENT_SECRET, "redirect_uris": [REDIRECT_URI]},
    }))
    get_settings.cache_clear()
    return TestClient(create_app())


def _register_and_login(client: TestClient, email: str = "alice@cho-fam.com", is_admin: bool = False):
    """UserAccount를 직접 만들어 승인 처리하고, /auth/login으로 로그인해 세션 쿠키를
    클라이언트에 심는다(회원가입 API는 관리자 승인 전에는 로그인을 안 시켜준다)."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        db.add(UserAccount(
            email=email, name="Alice", password_hash=hash_password("hunter22"),
            is_approved=True, is_admin=is_admin,
        ))
        db.commit()
    finally:
        db.close()
    r = client.post("/paas/api/v1/auth/login", json={"email": email, "password": "hunter22"})
    assert r.status_code == 200, r.text
    return email


def test_discovery_document_points_at_our_own_endpoints(provider_client):
    r = provider_client.get("/paas/.well-known/openid-configuration")
    assert r.status_code == 200
    body = r.json()
    assert body["issuer"] == "http://paas.test/paas"
    assert body["authorization_endpoint"] == "http://paas.test/paas/oauth2/authorize"
    assert body["token_endpoint"] == "http://paas.test/paas/oauth2/token"
    assert body["jwks_uri"] == "http://paas.test/paas/oauth2/jwks"


def test_jwks_exposes_rsa_public_key(provider_client):
    r = provider_client.get("/paas/oauth2/jwks")
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert len(keys) == 1
    assert keys[0]["kty"] == "RSA" and keys[0]["alg"] == "RS256" and keys[0]["kid"]


def test_authorize_without_session_redirects_to_console_login(provider_client):
    r = provider_client.get(
        "/paas/oauth2/authorize",
        params={"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "response_type": "code", "state": "xyz"},
        follow_redirects=False,
    )
    assert r.status_code == 307
    location = r.headers["location"]
    assert location.startswith("http://paas.test/console/#/login?next=")
    assert "oauth2%2Fauthorize" in location  # /paas/oauth2/authorize가 그대로 인코딩돼 들어있다


def test_authorize_with_session_issues_code_and_redirects_to_client(provider_client):
    _register_and_login(provider_client)
    r = provider_client.get(
        "/paas/oauth2/authorize",
        params={
            "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
            "response_type": "code", "state": "xyz", "nonce": "nnn",
        },
        follow_redirects=False,
    )
    assert r.status_code == 307
    location = r.headers["location"]
    assert location.startswith(REDIRECT_URI)
    assert "code=" in location and "state=xyz" in location


def test_authorize_rejects_unknown_client(provider_client):
    r = provider_client.get(
        "/paas/oauth2/authorize",
        params={"client_id": "no-such-client", "redirect_uri": REDIRECT_URI, "response_type": "code"},
    )
    assert r.status_code == 400


def test_authorize_rejects_unregistered_redirect_uri(provider_client):
    r = provider_client.get(
        "/paas/oauth2/authorize",
        params={"client_id": CLIENT_ID, "redirect_uri": "https://evil.example.com/callback", "response_type": "code"},
    )
    assert r.status_code == 400


def _get_code(client: TestClient) -> str:
    from urllib.parse import parse_qs, urlparse
    r = client.get(
        "/paas/oauth2/authorize",
        params={"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "response_type": "code", "state": "s"},
        follow_redirects=False,
    )
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


def test_token_exchange_returns_id_token(provider_client):
    _register_and_login(provider_client, is_admin=True)
    code = _get_code(provider_client)
    r = provider_client.post("/paas/oauth2/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    claims = oidc_provider.decode_id_token(body["id_token"])
    assert claims["email"] == "alice@cho-fam.com"
    assert claims["preferred_username"] == "alice@cho-fam.com"
    assert claims["realm_access"]["roles"] == ["paas-admin"]  # is_admin=True


def test_token_exchange_supports_http_basic_client_auth(provider_client):
    import base64
    _register_and_login(provider_client)
    code = _get_code(provider_client)
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = provider_client.post(
        "/paas/oauth2/token",
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
        headers={"Authorization": f"Basic {basic}"},
    )
    assert r.status_code == 200, r.text


def test_token_exchange_rejects_wrong_client_secret(provider_client):
    _register_and_login(provider_client)
    code = _get_code(provider_client)
    r = provider_client.post("/paas/oauth2/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID, "client_secret": "wrong",
    })
    assert r.status_code == 400


def test_token_exchange_rejects_redirect_uri_mismatch(provider_client):
    _register_and_login(provider_client)
    code = _get_code(provider_client)
    r = provider_client.post("/paas/oauth2/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI + "/different",
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    })
    assert r.status_code == 400


def test_code_is_single_use(provider_client):
    """같은 code로 두 번 교환하면 두 번째는 반드시 실패한다 — 탈취된 code 재사용 방지."""
    _register_and_login(provider_client)
    code = _get_code(provider_client)
    body = {
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    }
    assert provider_client.post("/paas/oauth2/token", data=body).status_code == 200
    assert provider_client.post("/paas/oauth2/token", data=body).status_code == 400


def test_userinfo_reflects_id_token_claims(provider_client):
    _register_and_login(provider_client)
    code = _get_code(provider_client)
    token = provider_client.post("/paas/oauth2/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    }).json()["id_token"]
    r = provider_client.get("/paas/oauth2/userinfo", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["email"] == "alice@cho-fam.com"


def test_userinfo_rejects_missing_bearer(provider_client):
    assert provider_client.get("/paas/oauth2/userinfo").status_code == 401


def test_provider_disabled_by_default(fresh_settings):
    """PAAS_OIDC_PROVIDER_ENABLED 없이는 라우터 자체가 안 붙는다 — 새 공격 표면을
    옵트인 없이 열어두지 않는다."""
    get_settings.cache_clear()
    c = TestClient(create_app())
    assert c.get("/paas/.well-known/openid-configuration").status_code == 404


def test_self_issued_token_is_accepted_by_authenticate_bearer(provider_client, monkeypatch):
    """핵심 통합 지점 — oidc_issuer를 paas 자신으로 맞추면, 기존(Keycloak용) 검증
    코드가 우리가 발급한 토큰을 코드 변경 없이 그대로 받아들여야 한다."""
    _register_and_login(provider_client, is_admin=True)
    code = _get_code(provider_client)
    id_token = provider_client.post("/paas/oauth2/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    }).json()["id_token"]

    monkeypatch.setenv("PAAS_OIDC_ISSUER", oidc_provider.issuer())
    get_settings.cache_clear()

    class _FakeSigningKey:
        key = oidc_provider._get_signing_key().public_key()

    class _FakeJwkClient:
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey()

    monkeypatch.setattr(security, "_jwk_client", _FakeJwkClient())
    key = security.authenticate_bearer(id_token)
    assert key.name == "alice@cho-fam.com"
    assert key.is_admin is True
