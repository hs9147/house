"""아웃바운드 검증 — 사내/사외 판정, 유출 요소 탐지, 게이트웨이가 실제로 보내는 것."""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.models import Module, ModuleType
from app.services import egress
from app.services import modules as modules_service

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


def _module(name: str, type_: ModuleType, config: dict) -> Module:
    return Module(name=name, type=type_, config=modules_service.encrypt_config(config))


# --- 사내/사외 판정 ---

@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.0.1", "localhost",
    "fileserver",                 # 단일 라벨 — 사내 DNS로만 풀린다
    "wiki.internal", "app.local", "erp.corp",
])
def test_internal_hosts(host, fresh_settings):
    assert egress.is_internal_host(host) is True


@pytest.mark.parametrize("host", ["api.openai.com", "example.com", "8.8.8.8", "sub.vendor.co.kr"])
def test_external_hosts(host, fresh_settings):
    assert egress.is_internal_host(host) is False


def test_platform_own_hosts_count_as_internal(monkeypatch, fresh_settings):
    """공개 도메인처럼 보여도 플랫폼·Gitea 자신이면 망을 벗어나지 않는다."""
    monkeypatch.setenv("PAAS_GITEA_URL", "https://git.example.com")
    get_settings.cache_clear()
    assert egress.is_internal_host("git.example.com") is True


def test_internal_domain_suffix_setting(monkeypatch, fresh_settings):
    """사내인데 공개 도메인처럼 생긴 주소는 설정으로 알려 줘야 한다."""
    assert egress.is_internal_host("erp.company.co.kr") is False
    monkeypatch.setenv("PAAS_INTERNAL_DOMAINS", "company.co.kr, other.example")
    get_settings.cache_clear()
    assert egress.is_internal_host("erp.company.co.kr") is True
    assert egress.is_internal_host("company.co.kr") is True
    assert egress.is_internal_host("notcompany.co.kr") is False


# --- 모듈 점검 ---

def test_external_https_module_is_secured(fresh_settings):
    report = egress.inspect_module(
        _module("weather", ModuleType.external_api, {"url": "https://api.weather.com/v1"}))
    assert report["scope"] == "external"
    assert report["secured"] is True
    assert report["findings"] == []
    # 사외 대상에는 호출자 신원을 싣지 않는다
    assert not any("calling-agent" in s for s in report["platform_sends"])


def test_internal_module_reports_what_the_gateway_attaches(fresh_settings):
    report = egress.inspect_module(
        _module("erp", ModuleType.internal_api, {"url": "http://10.0.0.9/api", "api_key": "k"}))
    assert report["scope"] == "internal" and report["secured"] is True
    assert any("calling-agent" in s for s in report["platform_sends"])
    # 사내라 평문 http는 지적하지 않는다(망을 벗어나지 않는다)
    assert report["findings"] == []


def test_plaintext_external_is_flagged(fresh_settings):
    report = egress.inspect_module(
        _module("vendor", ModuleType.external_api, {"url": "http://api.vendor.com/v1"}))
    assert report["secured"] is False
    assert any("평문" in f for f in report["findings"])


def test_credentials_in_url_are_flagged(fresh_settings):
    in_userinfo = egress.inspect_module(
        _module("a", ModuleType.external_api, {"url": "https://u:p@api.vendor.com/v1"}))
    assert in_userinfo["secured"] is False
    assert any("자격증명이 박혀" in f for f in in_userinfo["findings"])

    in_query = egress.inspect_module(
        _module("b", ModuleType.external_api, {"url": "https://api.vendor.com/v1?api_key=abc"}))
    assert in_query["secured"] is False
    assert any("쿼리" in f for f in in_query["findings"])


def test_local_modules_have_nothing_to_leak(fresh_settings):
    report = egress.inspect_module(
        _module("db", ModuleType.database, {"dsn": "postgres://u:p@10.0.0.2/x"}))
    assert report["scope"] == "local" and report["secured"] is True


def test_missing_url_is_unknown_not_secured(fresh_settings):
    report = egress.inspect_module(_module("empty", ModuleType.external_api, {}))
    assert report["scope"] == "unknown" and report["secured"] is False


# --- 실제 아웃바운드 경로 ---

def test_proxy_forwards_only_safe_headers(monkeypatch, fresh_settings):
    """들어온 헤더를 통째로 넘기면 호출자의 x-api-key·cookie가 대상에게 그대로 나간다."""
    sent = {}

    class _FakeResponse:
        content, status_code, headers = b"{}", 200, {"content-type": "application/json"}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, headers, params, content):
            sent.update(headers=headers, url=url)
            return _FakeResponse()

    from app.api import proxy_gateway

    monkeypatch.setattr(proxy_gateway.httpx, "AsyncClient", lambda **kw: _FakeClient())
    c = TestClient(create_app())
    c.post(f"{API}/modules", json={
        "name": "vendor", "type": "external_api", "config": {"url": "https://api.vendor.com"},
    }, headers=ADMIN)

    r = c.post(f"{API}/proxy/modules/vendor/search", headers={
        **ADMIN, "content-type": "application/json", "cookie": "paas_session=secret",
        "authorization": "Bearer user-token", "x-forwarded-for": "10.0.0.7",
    }, json={"q": "x"})
    assert r.status_code == 200

    forwarded = {k.lower() for k in sent["headers"]}
    assert "content-type" in forwarded
    for leaked in ("x-api-key", "cookie", "authorization", "x-forwarded-for"):
        assert leaked not in forwarded, f"{leaked}가 대상에게 나갔다"


def test_a2a_attaches_caller_identity_only_for_internal_targets(monkeypatch, fresh_settings):
    """호출자 신원(대개 이메일)을 사외 대상에 붙이면 그 자체가 사내 정보 유출이다."""
    import httpx

    seen = {}

    class _FakeResponse:
        status_code, headers = 200, {"content-type": "application/json"}

        def json(self):
            return {"ok": True}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            seen[url] = headers
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    c = TestClient(create_app())
    for name, url in (("vendor", "https://api.vendor.com"), ("erp", "http://10.0.0.9/api")):
        assert c.post(f"{API}/modules", json={
            "name": name, "type": "external_api", "config": {"url": url, "api_key": "k"},
        }, headers=ADMIN).status_code == 201
        assert c.post(f"{API}/a2a/agents/{name}/task", headers=ADMIN,
                      json={"capability": "invoke_api", "input": {}}).status_code == 200

    external = seen["https://api.vendor.com"]
    internal = seen["http://10.0.0.9/api"]
    assert "x-paas-calling-agent" not in external
    assert "x-paas-a2a-gateway" not in external
    assert external["authorization"] == "Bearer k"   # 대상 자격증명은 그대로 실린다
    assert internal["x-paas-calling-agent"] == "bootstrap-admin"


def test_modules_list_carries_the_verdict(monkeypatch, fresh_settings):
    """화면 배지가 읽는 값 — 목록에 함께 실려 나간다."""
    c = TestClient(create_app())
    c.post(f"{API}/modules", json={
        "name": "vendor", "type": "external_api", "config": {"url": "https://api.vendor.com"},
    }, headers=ADMIN)
    c.post(f"{API}/modules", json={
        "name": "leaky", "type": "external_api", "config": {"url": "http://api.vendor.com"},
    }, headers=ADMIN)

    rows = {m["name"]: m["egress"] for m in c.get(f"{API}/modules", headers=ADMIN).json()}
    assert rows["vendor"]["secured"] is True and rows["vendor"]["scope"] == "external"
    assert rows["leaky"]["secured"] is False and rows["leaky"]["findings"]
