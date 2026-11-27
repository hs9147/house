"""GPAX 계정 로그인으로 MCP 연결(services/mcp_oauth.py) — MCP 클라이언트가 실제로 밟는 순서
그대로: 401 → 보호 자원 메타데이터 → 발급자 메타데이터 → 등록 → 로그인·승인 → 토큰 → MCP 호출.

울타리도 함께 본다: PKCE·code 1회·승인 표(CSRF)·대상(aud)·MCP 밖 거부·끊기 즉시 반영."""
import base64
import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.models import UserAccount
from app.security import hash_password
from app.services import oidc_provider

BASE = "http://paas.test"
HUB = f"{BASE}/paas/api/v1/mcp/hub"
REDIRECT = "http://127.0.0.1/callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture(autouse=True)
def _reset_provider_singletons():
    oidc_provider._signing_key = None
    oidc_provider._kid = None
    yield
    oidc_provider._signing_key = None
    oidc_provider._kid = None


@pytest.fixture
def client(monkeypatch, fresh_settings, tmp_path):
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("PAAS_MCP_OAUTH_ENABLED", "true")  # 기본은 꺼짐(HTTPS가 생길 때까지)
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", BASE)
    # 운영과 같은 모양 — Gitea 백채널은 localhost. MCP 쪽은 이것과 상관없이 공개 주소여야 한다.
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_BACKCHANNEL_URL", "http://localhost:7000/paas")
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH", str(tmp_path / "key.pem"))
    get_settings.cache_clear()
    return TestClient(create_app())


def _login(c: TestClient, email="alice@cho-fam.com", is_admin=False):
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        db.add(UserAccount(email=email, name="Alice", password_hash=hash_password("hunter22"),
                           is_approved=True, is_admin=is_admin))
        db.commit()
    finally:
        db.close()
    r = c.post("/paas/api/v1/auth/login", json={"email": email, "password": "hunter22"})
    assert r.status_code == 200, r.text
    return r.json()["key"]


def _register(c: TestClient, uris=(REDIRECT,), name="Claude Code"):
    r = c.post("/paas/oauth/register", json={
        "redirect_uris": list(uris), "client_name": name, "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def _auth_params(client_id, redirect=REDIRECT, resource=HUB, state="st-1"):
    return {"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": state,
            "resource": resource, "scope": "mcp"}


def _consent_form(page: str) -> dict:
    return dict(re.findall(r"name='(\w+)' value='([^']*)'", page))


def _approve(c, client_id, redirect=REDIRECT, resource=HUB) -> str:
    r = c.get("/paas/oauth/authorize", params=_auth_params(client_id, redirect, resource))
    assert r.status_code == 200, r.text
    form = _consent_form(r.text)
    r = c.post("/paas/oauth/authorize", data={**form, "decision": "approve"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert q["state"] == ["st-1"]
    return q["code"][0]


def _tokens(c, client_id, code, redirect=REDIRECT, verifier=VERIFIER):
    return c.post("/paas/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier, "resource": HUB})


def _hub(c, token):
    return c.post("/paas/api/v1/mcp/hub", headers={"Authorization": f"Bearer {token}"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})


def test_unauthenticated_mcp_points_at_login(client):
    r = client.post("/paas/api/v1/mcp/hub", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401
    header = r.headers["www-authenticate"]
    assert header.startswith("Bearer ")
    m = re.search(r'resource_metadata="([^"]+)"', header)
    assert m and m.group(1) == f"{BASE}/paas/.well-known/oauth-protected-resource/api/v1/mcp/hub"

    prm = client.get("/paas/.well-known/oauth-protected-resource/api/v1/mcp/hub").json()
    assert prm["resource"] == HUB
    assert prm["authorization_servers"] == [f"{BASE}/paas/oauth"]
    # IIS가 뿌리 경로를 넘겨 줄 때 받는 형태도 같은 문서
    assert client.get("/.well-known/oauth-protected-resource/paas/api/v1/mcp/hub").json() == prm


def test_metadata_is_separate_from_gitea_discovery(client):
    as_meta = client.get("/paas/oauth/.well-known/oauth-authorization-server").json()
    assert as_meta["issuer"] == f"{BASE}/paas/oauth"
    assert as_meta["token_endpoint"] == f"{BASE}/paas/oauth/token"
    assert as_meta["registration_endpoint"] == f"{BASE}/paas/oauth/register"
    assert as_meta["code_challenge_methods_supported"] == ["S256"]
    for path in ("/paas/oauth/.well-known/openid-configuration",
                 "/.well-known/oauth-authorization-server/paas/oauth",
                 "/.well-known/openid-configuration/paas/oauth"):
        assert client.get(path).json() == as_meta, path
    # Gitea 쪽 디스커버리는 그대로(백채널) — MCP 클라이언트가 이걸 집으면 토큰 교환이 자기 PC로 간다
    assert client.get("/paas/.well-known/openid-configuration").json()["token_endpoint"] ==         "http://localhost:7000/paas/oauth2/token"


@pytest.mark.parametrize("uri", ["http://evil.example/cb", "javascript:alert(1)",
                                 "https://ok.example/cb#frag", "file:///etc/passwd"])
def test_register_rejects_unsafe_redirects(client, uri):
    r = client.post("/paas/oauth/register", json={"redirect_uris": [uri]})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_redirect_uri"


def test_authorize_sends_to_console_login_first(client):
    cid = _register(client)
    r = client.get("/paas/oauth/authorize", params=_auth_params(cid), follow_redirects=False)
    assert r.status_code == 307
    loc = r.headers["location"]
    assert loc.startswith(f"{BASE}/console/#/login?next=")
    nxt = parse_qs(urlparse(loc.replace("#/login", "")).query)["next"][0]
    assert nxt.startswith("/paas/oauth/authorize?") and cid in nxt


def test_authorize_rejects_unknown_client_and_missing_pkce(client):
    _login(client)
    r = client.get("/paas/oauth/authorize", params=_auth_params("nope"))
    assert r.status_code == 400 and "등록되지 않은" in r.text
    cid = _register(client)
    params = _auth_params(cid)
    params.pop("code_challenge")
    r = client.get("/paas/oauth/authorize", params=params)
    assert r.status_code == 400 and "PKCE" in r.text
    r = client.get("/paas/oauth/authorize",
                   params=_auth_params(cid, redirect="http://127.0.0.1/other"))
    assert r.status_code == 400
    r = client.get("/paas/oauth/authorize",
                   params=_auth_params(cid, resource="http://other.test/paas/api/v1/mcp/hub"))
    assert r.status_code == 400


def test_full_flow_then_mcp_works_and_console_api_does_not(client):
    _login(client)
    cid = _register(client)
    page = client.get("/paas/oauth/authorize", params=_auth_params(cid))
    assert "Claude Code" in page.text and "alice@cho-fam.com" in page.text
    assert page.headers["x-frame-options"] == "DENY"

    code = _approve(client, cid)
    assert _tokens(client, cid, code, verifier="w" * 64).json()["error"] == "invalid_grant"
    # 틀린 verifier로 한 번 시도한 code는 이미 지워졌다 — 맞는 값으로도 못 쓴다
    assert _tokens(client, cid, code).json()["error"] == "invalid_grant"

    code = _approve(client, cid)
    r = _tokens(client, cid, code)
    assert r.status_code == 200, r.text
    tok = r.json()
    assert tok["token_type"] == "Bearer" and tok["refresh_token"].startswith("mcpr_")
    assert r.headers["cache-control"] == "no-store"

    client.cookies.clear()  # 이제 브라우저 세션 없이 토큰만으로
    assert _hub(client, tok["access_token"]).status_code == 200
    # MCP 밖(콘솔 API)은 이 토큰을 받지 않는다
    assert client.get("/paas/api/v1/auth/me",
                      headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 401
    # 허브용 토큰으로 다른 MCP·게이트웨이는 못 연다(aud)
    assert client.get("/paas/api/v1/a2a/agents",
                      headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 401


def test_loopback_port_may_differ(client):
    _login(client)
    cid = _register(client)
    code = _approve(client, cid, redirect="http://127.0.0.1:54321/callback")
    assert _tokens(client, cid, code, redirect="http://127.0.0.1:54321/callback").status_code == 200


def test_consent_needs_the_ticket_from_this_page(client):
    _login(client)
    cid = _register(client)
    form = _consent_form(client.get("/paas/oauth/authorize", params=_auth_params(cid)).text)
    # 표 없이(다른 사이트가 폼만 흉내 낸 경우)
    no_ticket = {k: v for k, v in form.items() if k != "ticket"}
    r = client.post("/paas/oauth/authorize", data={**no_ticket, "decision": "approve"},
                    follow_redirects=False)
    assert r.status_code == 403
    # 표는 있지만 돌아갈 곳을 바꾼 경우
    r = client.post("/paas/oauth/authorize",
                    data={**form, "redirect_uri": "http://127.0.0.1:9/callback", "decision": "approve"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_deny_returns_access_denied(client):
    _login(client)
    cid = _register(client)
    form = _consent_form(client.get("/paas/oauth/authorize", params=_auth_params(cid)).text)
    r = client.post("/paas/oauth/authorize", data={**form, "decision": "deny"},
                    follow_redirects=False)
    assert r.status_code == 303
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["error"] == ["access_denied"] and "code" not in q


def test_refresh_rotates_and_old_value_dies(client):
    _login(client)
    cid = _register(client)
    tok = _tokens(client, cid, _approve(client, cid)).json()
    r = client.post("/paas/oauth/token", data={"grant_type": "refresh_token",
                                               "refresh_token": tok["refresh_token"], "client_id": cid})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["refresh_token"] != tok["refresh_token"]
    again = client.post("/paas/oauth/token", data={"grant_type": "refresh_token",
                                                   "refresh_token": tok["refresh_token"], "client_id": cid})
    assert again.json()["error"] == "invalid_grant"
    client.cookies.clear()
    assert _hub(client, new["access_token"]).status_code == 200


def test_grants_list_and_revoke_cut_off_immediately(client):
    session = _login(client)
    cid = _register(client)
    tok = _tokens(client, cid, _approve(client, cid)).json()
    # 같은 앱으로 다시 승인하면 옛 승인을 대신한다 — 목록에 쌓이지 않는다
    tok = _tokens(client, cid, _approve(client, cid)).json()

    h = {"x-api-key": session}
    listed = client.get("/paas/api/v1/mcp/oauth/grants", headers=h).json()
    assert listed["enabled"] is True
    assert [g["client_name"] for g in listed["grants"]] == ["Claude Code"]
    assert listed["grants"][0]["resource"] == HUB

    gid = listed["grants"][0]["id"]
    assert client.delete(f"/paas/api/v1/mcp/oauth/grants/{gid}", headers=h).status_code == 204
    client.cookies.clear()
    r = _hub(client, tok["access_token"])  # 아직 만료 전인 액세스 토큰도 바로 막힌다
    assert r.status_code == 401
    assert 'error="invalid_token"' in r.headers["www-authenticate"]


def test_personal_token_still_works(client):
    session = _login(client)
    r = client.post("/paas/api/v1/mcp/tokens", headers={"x-api-key": session}, json={"label": "pc"})
    assert r.status_code == 201, r.text
    client.cookies.clear()
    assert _hub(client, r.json()["token"]).status_code == 200


def test_disabled_without_public_url(monkeypatch, fresh_settings, tmp_path):
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("PAAS_MCP_OAUTH_ENABLED", "true")
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", "")
    monkeypatch.setenv("PAAS_OIDC_ISSUER", "http://paas.test/paas")
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_BACKCHANNEL_URL", "")
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH", str(tmp_path / "key.pem"))
    get_settings.cache_clear()
    c = TestClient(create_app())
    assert c.get("/paas/oauth/.well-known/oauth-authorization-server").status_code == 404
    r = c.post("/paas/api/v1/mcp/hub", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401
    assert "bearer" not in r.headers.get("www-authenticate", "").lower()
    assert json.loads(r.text)["detail"]


def test_login_helper_download_has_this_server_address(client):
    """도우미는 로그인 없이 받는다(비밀이 없다) — 서버 주소를 채워 내려 준다."""
    r = client.get("/paas/api/v1/mcp/paas-mcp.mjs")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert f"'{BASE}'" in r.text and "__PAAS_BASE__" not in r.text
    assert "headersHelper" in r.text  # Claude Code에 헤더만 건네는 방식


def test_login_helper_is_unavailable_when_oauth_is_off(monkeypatch, fresh_settings, tmp_path):
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "true")
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", BASE)
    monkeypatch.setenv("PAAS_MCP_OAUTH_ENABLED", "false")
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH", str(tmp_path / "key.pem"))
    get_settings.cache_clear()
    assert TestClient(create_app()).get("/paas/api/v1/mcp/paas-mcp.mjs").status_code == 404


def test_off_by_default_so_401_carries_no_login_hint(monkeypatch, fresh_settings, tmp_path):
    """HTTPS가 생기기 전에는 꺼 둔다 — 켜 두면 토큰이 만료된 클라이언트가 OAuth를 시도하다 TLS 오류만 낸다."""
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_ENABLED", "true")
    monkeypatch.delenv("PAAS_MCP_OAUTH_ENABLED", raising=False)
    monkeypatch.setenv("PAAS_PLATFORM_PUBLIC_URL", BASE)
    monkeypatch.setenv("PAAS_OIDC_PROVIDER_SIGNING_KEY_PATH", str(tmp_path / "key.pem"))
    get_settings.cache_clear()
    assert get_settings().mcp_oauth_enabled is False
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/mcp/hub", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401
    assert "bearer" not in r.headers.get("www-authenticate", "").lower()
    assert c.get("/paas/api/v1/mcp/paas-mcp.mjs").status_code == 404
    assert c.get("/paas/oauth/.well-known/oauth-authorization-server").status_code == 404
