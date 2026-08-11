"""paas 자체 OIDC Provider(Keycloak 없이 직접 구현) — Authorization Code 플로우 전체와,
발급한 토큰이 기존 authenticate_bearer(Keycloak용으로 짜여진 검증 코드)에 그대로
먹히는지까지 확인한다(핵심 요구사항: oidc_issuer를 paas 자신으로 돌려도 코드 변경 없음)."""
import json

import pytest
from fastapi import HTTPException
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


@pytest.fixture
def split_channel_client(monkeypatch, fresh_settings, tmp_path):
    """공개 도메인의 binding이 이 플랫폼을 안 가리켜 https://공개도메인/paas 로는 못
    들어오는 구성 — 백채널만 사내 주소(평문 http)로 뺀다."""
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", "https://public.example.com")
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_BACKCHANNEL_URL", "http://10.0.0.5:7000/paas")
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH", str(tmp_path / "k.pem"))
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_CLIENTS", json.dumps({
        CLIENT_ID: {"secret": CLIENT_SECRET, "redirect_uris": [REDIRECT_URI]},
    }))
    get_settings.cache_clear()
    return TestClient(create_app())


def test_backchannel_url_splits_server_calls_from_browser_calls(split_channel_client):
    """issuer·token·jwks는 사내 주소(클라이언트가 서버에서 부름 — TLS 자체를 안 탄다),
    authorization_endpoint는 공개 주소(브라우저가 연다). 클라이언트(go-oidc 등)는
    discovery URL과 issuer가 같은지만 확인하므로 이 분리가 규약상 문제없다."""
    body = split_channel_client.get("/paas/.well-known/openid-configuration").json()
    assert body["issuer"] == "http://10.0.0.5:7000/paas"
    assert body["token_endpoint"] == "http://10.0.0.5:7000/paas/oauth2/token"
    assert body["jwks_uri"] == "http://10.0.0.5:7000/paas/oauth2/jwks"
    # 브라우저가 가는 곳만 공개 주소여야 한다 — 사내 주소로 보내면 사용자가 못 연다.
    assert body["authorization_endpoint"] == "https://public.example.com/paas/oauth2/authorize"


def test_split_channel_token_passes_bearer_auth_without_oidc_issuer_set(split_channel_client):
    """회귀: 백채널 분리 구성(사이트=https, 내부 인증=http)에서 우리 토큰이 401이 됐다.

    토큰은 issuer()(=백채널 주소)로 발급되는데 검증은 oidc_issuer 설정값으로 하고 있어
    두 값이 조용히 어긋났다. 우리 토큰은 우리가 발급에 쓴 값으로 검증해야 하고, 그러면
    내장 Provider만 쓰는 구성에서는 PAAS_OIDC_ISSUER를 설정할 필요가 아예 없다.
    """
    assert not get_settings().oidc_issuer  # 이 fixture는 oidc_issuer를 설정하지 않는다

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        db.add(UserAccount(
            email="alice@cho-fam.com", name="A", password_hash=hash_password("hunter22"),
            is_approved=True, is_admin=True,
        ))
        db.commit()
    finally:
        db.close()
    session_token = split_channel_client.post(
        "/paas/api/v1/auth/login",
        json={"email": "alice@cho-fam.com", "password": "hunter22"},
    ).json()["key"]
    split_channel_client.cookies.set("paas_session", session_token)

    code = _get_code(split_channel_client)
    id_token = split_channel_client.post("/paas/oauth2/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    }).json()["id_token"]

    key = security.authenticate_bearer(id_token)
    assert key.name == "alice@cho-fam.com" and key.is_admin is True


def test_split_channel_tokens_carry_the_backchannel_issuer(split_channel_client):
    """발급 토큰의 iss는 백채널 issuer여야 한다 — 클라이언트는 자기가 아는 provider
    issuer와 토큰의 iss가 같은지 확인하므로, 여기가 어긋나면 로그인이 거부된다."""
    # platform_public_url이 https라 로그인 쿠키가 secure로 발급된다(운영에서 옳은 동작).
    # TestClient는 http로 요청하므로 그 쿠키를 자동 보관하지 않는다 — 값을 직접 심는다.
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        db.add(UserAccount(
            email="alice@cho-fam.com", name="Alice",
            password_hash=hash_password("hunter22"), is_approved=True, is_admin=False,
        ))
        db.commit()
    finally:
        db.close()
    session_token = split_channel_client.post(
        "/paas/api/v1/auth/login",
        json={"email": "alice@cho-fam.com", "password": "hunter22"},
    ).json()["key"]
    split_channel_client.cookies.set("paas_session", session_token)

    code = _get_code(split_channel_client)
    token = split_channel_client.post("/paas/oauth2/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    }).json()["id_token"]
    assert oidc_provider.decode_id_token(token)["iss"] == "http://10.0.0.5:7000/paas"


def _login_cookie_flags(client: TestClient) -> str:
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        db.add(UserAccount(
            email="alice@cho-fam.com", name="A", password_hash=hash_password("hunter22"),
            is_approved=True, is_admin=False,
        ))
        db.commit()
    finally:
        db.close()
    r = client.post("/paas/api/v1/auth/login",
                    json={"email": "alice@cho-fam.com", "password": "hunter22"})
    assert r.status_code == 200, r.text
    return r.headers["set-cookie"]


def test_session_cookie_is_not_secure_on_a_plain_http_deployment(monkeypatch, fresh_settings):
    """443을 못 써서 전 구간 http(80)로만 운영하는 구성 — 세션 쿠키에 secure가 붙으면
    브라우저가 저장 자체를 안 한다. 그러면 로그인은 성공한 것처럼 보이는데 authorize는
    계속 미로그인으로 판단해 로그인 화면으로 되돌리는 무한 루프가 된다."""
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", "http://paas.example.com")
    get_settings.cache_clear()
    cookie = _login_cookie_flags(TestClient(create_app()))
    assert "paas_session=" in cookie
    assert "secure" not in cookie.lower()
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()  # strict면 Gitea발 이동에 안 실린다


def test_session_cookie_is_secure_on_an_https_deployment(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", "https://paas.example.com")
    get_settings.cache_clear()
    cookie = _login_cookie_flags(TestClient(create_app()))
    assert "secure" in cookie.lower()


def test_issuer_requires_an_absolute_url(monkeypatch, fresh_settings):
    """발급자 주소가 없으면 상대 경로("/paas")가 섞인 잘못된 디스커버리 문서를 내주는
    대신 분명히 실패해야 한다 — 그걸 받은 Gitea 쪽 오류는 원인 파악이 훨씬 어렵다."""
    monkeypatch.delenv("PAAS_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("PAAS_PLATFORM_PUBLIC_URL", raising=False)
    get_settings.cache_clear()
    with pytest.raises(oidc_provider.OidcProviderError, match="절대 주소"):
        oidc_provider.issuer()


def test_discovery_fails_loudly_when_issuer_is_unset(monkeypatch, fresh_settings, tmp_path):
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "true")
    monkeypatch.delenv("PAAS_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("PAAS_PLATFORM_PUBLIC_URL", raising=False)
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH", str(tmp_path / "k.pem"))
    get_settings.cache_clear()
    r = TestClient(create_app()).get("/paas/.well-known/openid-configuration")
    assert r.status_code == 500
    assert "PAAS_OIDC_ISSUER" in r.json()["detail"]


def test_external_token_is_not_mistaken_for_ours_when_provider_is_also_enabled(
    monkeypatch, fresh_settings, tmp_path,
):
    """회귀: 내장 Provider를 켜 둔 채로 외부 Keycloak을 신뢰 발급자로 쓰는 구성.

    "우리 토큰인가"를 주소(oidc_issuer)로 판별하면, oidc_issuer가 설정돼 있기만 하면
    항상 참이 돼(자기 자신과 비교하는 꼴) Keycloak 토큰까지 우리 키로 검증하려 든다 —
    유효한 Keycloak 토큰이 전부 401이 된다. 판별 기준은 토큰이 실제로 우리 키로
    서명됐는지(kid)여야 한다.
    """
    import time

    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", "https://paas.test")
    monkeypatch.setenv("PAAS_OIDC_ISSUER", "https://sso.external.test/realms/x")
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH", str(tmp_path / "k.pem"))
    get_settings.cache_clear()

    external_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = pyjwt.encode({
        "iss": "https://sso.external.test/realms/x", "sub": "u1",
        "preferred_username": "hong", "exp": int(time.time()) + 3600,
        "realm_access": {"roles": ["paas-admin"]},
    }, external_key, algorithm="RS256", headers={"kid": "keycloak-key-1"})

    assert security._issued_by_our_own_provider(token) is False

    called = []

    class _FakeJwkClient:
        def get_signing_key_from_jwt(self, tok):
            called.append(tok)
            return type("K", (), {"key": external_key.public_key()})()

    monkeypatch.setattr(security, "_get_jwk_client", lambda: _FakeJwkClient())
    key = security.authenticate_bearer(token)
    assert key.name == "hong" and key.is_admin is True
    assert called, "외부 발급자 토큰인데 JWKS를 조회하지 않았다"


def test_forged_kid_does_not_bypass_signature_check(monkeypatch, fresh_settings, tmp_path):
    """kid는 서명 전 헤더라 위조할 수 있다 — 우리 kid를 달고 와도 서명이 우리 키가
    아니면 반드시 떨어져야 한다(키 선택에만 쓰고 신뢰하지는 않는다)."""
    import time

    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", "https://paas.test")
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH", str(tmp_path / "k.pem"))
    get_settings.cache_clear()
    monkeypatch.setenv("PAAS_OIDC_ISSUER", oidc_provider.issuer())
    get_settings.cache_clear()

    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = pyjwt.encode({
        "iss": oidc_provider.issuer(), "sub": "evil@cho-fam.com",
        "preferred_username": "evil@cho-fam.com", "exp": int(time.time()) + 3600,
        "realm_access": {"roles": ["paas-admin"]},
    }, attacker_key, algorithm="RS256", headers={"kid": oidc_provider.key_id()})

    with pytest.raises(HTTPException) as exc:
        security.authenticate_bearer(forged)
    assert exc.value.status_code == 401


def test_provider_disabled_by_default(fresh_settings):
    """PAAS_OIDC_PROVIDER_ENABLED 없이는 라우터 자체가 안 붙는다 — 새 공격 표면을
    옵트인 없이 열어두지 않는다."""
    get_settings.cache_clear()
    c = TestClient(create_app())
    assert c.get("/paas/.well-known/openid-configuration").status_code == 404


def test_self_issued_token_is_verified_locally_without_fetching_jwks(provider_client, monkeypatch):
    """핵심 통합 지점 — oidc_issuer를 플랫폼 자신으로 맞추면 기존(Keycloak용) 검증
    코드가 우리 토큰을 그대로 받아들여야 하고, 그때 JWKS를 **네트워크로 가져오면 안 된다**.

    회귀: 자기 자신을 공개 주소로 다시 호출하면 (1) 그 주소의 서버 인증서가 안 맞을 때
    TLS 검증에서 실패하고("oidc url과 서버 인증서가 일치하지 않는 오류"), (2) 기본 JWKS
    경로가 Keycloak 규약이라 우리 엔드포인트와 달라 404가 나며, (3) 워커가 자기 요청
    처리 도중 자기를 동기 호출하게 된다. 개인키가 이미 로컬에 있으니 그럴 이유가 없다.
    """
    _register_and_login(provider_client, is_admin=True)
    code = _get_code(provider_client)
    id_token = provider_client.post("/paas/oauth2/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
    }).json()["id_token"]

    monkeypatch.setenv("PAAS_OIDC_ISSUER", oidc_provider.issuer())
    get_settings.cache_clear()

    # JWKS 조회를 시도하기만 해도 실패하게 만든다 — 로컬 키로만 검증해야 통과한다.
    def _must_not_fetch():
        raise AssertionError("자체 발급 토큰인데 JWKS를 네트워크로 가져오려 했다")
    monkeypatch.setattr(security, "_get_jwk_client", _must_not_fetch)

    key = security.authenticate_bearer(id_token)
    assert key.name == "alice@cho-fam.com"
    assert key.is_admin is True


def test_external_issuer_still_uses_jwks(monkeypatch, fresh_settings):
    """반대편 — 발급자가 외부(Keycloak 등)면 예전처럼 JWKS를 조회해야 한다.
    위 최적화가 외부 IdP 경로까지 잘라먹지 않았는지 확인한다."""
    import time

    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    external_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = "https://sso.external.test/realms/company"
    monkeypatch.setenv("PAAS_OIDC_ISSUER", issuer)
    monkeypatch.delenv("PAAS_OIDC_PROVIDER_ENABLED", raising=False)
    get_settings.cache_clear()

    token = pyjwt.encode({
        "iss": issuer, "sub": "u1", "preferred_username": "hong",
        "exp": int(time.time()) + 3600, "realm_access": {"roles": ["paas-admin"]},
    }, external_key, algorithm="RS256")

    called = []

    class _FakeJwkClient:
        def get_signing_key_from_jwt(self, tok):
            called.append(tok)
            return type("K", (), {"key": external_key.public_key()})()

    monkeypatch.setattr(security, "_get_jwk_client", lambda: _FakeJwkClient())
    key = security.authenticate_bearer(token)
    assert key.name == "hong" and key.is_admin is True
    assert called, "외부 발급자인데 JWKS를 조회하지 않았다"
