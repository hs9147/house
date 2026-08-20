"""외부 API 디렉터리 검색 — 필터링·정규화, /modules/search·/modules/import, admin 게이트."""
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import apisearch
from app.services import httpx_retry

ADMIN = {"x-api-key": "test-admin-key"}

FAKE_DIRECTORY = {
    "stripe.com": {
        "preferred": "1.0",
        "versions": {"1.0": {"info": {
            "title": "Stripe", "description": "Online payment processing",
            "x-providerName": "stripe.com", "x-apisguru-categories": ["financial"],
            "contact": {"url": "https://stripe.com"},
        }, "swaggerUrl": "https://api.apis.guru/v2/specs/stripe.com/1.0/swagger.json"}},
    },
    "googleapis.com:calendar": {
        "preferred": "v3",
        "versions": {"v3": {"info": {
            "title": "Calendar API", "description": "Manipulates events",
            "x-apisguru-categories": ["productivity"],
        }, "swaggerUrl": "https://api.apis.guru/v2/specs/google/calendar/v3/swagger.json"}},
    },
    # x-apisguru-categories가 아예 없는 항목 — 실제 디렉터리에 흔하다
    "acme.io": {
        "preferred": "1.0",
        "versions": {"1.0": {"info": {
            "title": "Acme", "description": "billing widgets",
        }, "swaggerUrl": "https://api.apis.guru/v2/specs/acme.io/1.0/swagger.json"}},
    },
}


class _Res:
    status_code = 200

    def json(self):
        return FAKE_DIRECTORY


def _stub_directory(monkeypatch):
    apisearch.clear_cache()
    httpx_retry.reset_breakers()
    monkeypatch.setattr(httpx_retry.httpx, "get", lambda url, **kw: _Res())


def test_normalize_module_name():
    assert apisearch.normalize_module_name("googleapis.com:calendar") == "googleapis-com-calendar"
    assert apisearch.normalize_module_name("Stripe API!!") == "stripe-api"
    assert apisearch.normalize_module_name(":::") == "api"


def test_search_filters_by_keyword(monkeypatch):
    _stub_directory(monkeypatch)
    hits = apisearch.search_apis("payment")
    assert [h["id"] for h in hits] == ["stripe.com"]
    # 카테고리로도 매칭
    assert apisearch.search_apis("productivity")[0]["title"] == "Calendar API"
    assert apisearch.search_apis("nonexistent-kw") == []
    assert apisearch.search_apis("  ") == []


def test_search_filters_by_category(monkeypatch):
    _stub_directory(monkeypatch)
    assert [h["id"] for h in apisearch.search_apis("", "financial")] == ["stripe.com"]
    # 키워드와 AND — 둘 다 맞아야 걸린다
    assert apisearch.search_apis("payment", "financial")[0]["id"] == "stripe.com"
    assert apisearch.search_apis("payment", "productivity") == []
    # 대소문자는 가리지 않는다
    assert apisearch.search_apis("", "FINANCIAL")[0]["id"] == "stripe.com"


def test_uncategorized_entries_are_selectable(monkeypatch):
    """카테고리가 없는 항목은 카테고리 이름으로는 영영 걸리지 않는다 — 고를 값이 필요하다."""
    _stub_directory(monkeypatch)
    assert [h["id"] for h in apisearch.search_apis("", apisearch.UNCATEGORIZED)] == ["acme.io"]
    # "기타"를 골라도 키워드는 그대로 걸린다
    assert apisearch.search_apis("widget", apisearch.UNCATEGORIZED)[0]["id"] == "acme.io"
    assert apisearch.search_apis("calendar", apisearch.UNCATEGORIZED) == []


def test_no_condition_returns_nothing(monkeypatch):
    """기본값은 전체지만, 아무 조건도 없으면 디렉터리를 통째로 쏟아내지 않는다."""
    _stub_directory(monkeypatch)
    assert apisearch.search_apis("", "") == []
    assert apisearch.search_apis("   ", "  ") == []


def test_category_list_comes_from_the_directory(monkeypatch):
    _stub_directory(monkeypatch)
    items = apisearch.list_categories()
    assert items == [
        {"name": "financial", "count": 1},
        {"name": "productivity", "count": 1},
        {"name": "기타", "count": 1},
    ]


def test_category_endpoints(monkeypatch):
    _stub_directory(monkeypatch)
    c = TestClient(create_app())
    body = c.get("/paas/api/v1/modules/search/categories", headers=ADMIN).json()
    assert body["uncategorized_label"] == "기타"
    assert [i["name"] for i in body["categories"]] == ["financial", "productivity", "기타"]

    hits = c.get("/paas/api/v1/modules/search",
                 params={"category": "financial"}, headers=ADMIN).json()["results"]
    assert [h["id"] for h in hits] == ["stripe.com"]
    # 카테고리만으로도 검색된다(keyword 없이)
    assert c.get("/paas/api/v1/modules/search",
                 params={"category": "기타"}, headers=ADMIN).json()["results"][0]["id"] == "acme.io"


def test_search_endpoint_admin_only(monkeypatch):
    _stub_directory(monkeypatch)
    c = TestClient(create_app())
    r = c.get("/paas/api/v1/modules/search", params={"keyword": "payment"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["results"][0]["id"] == "stripe.com"

    member = c.post("/paas/api/v1/keys", json={"name": "dev"}, headers=ADMIN).json()["key"]
    r2 = c.get("/paas/api/v1/modules/search", params={"keyword": "x"},
               headers={"x-api-key": member})
    assert r2.status_code == 403


def test_import_creates_external_api_module(monkeypatch):
    _stub_directory(monkeypatch)
    c = TestClient(create_app())
    r = c.post("/paas/api/v1/modules/import", json={
        "name": "googleapis.com:calendar",
        "url": "https://api.apis.guru/v2/specs/google/calendar/v3/swagger.json",
        "category": "productivity",
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "googleapis-com-calendar"
    assert body["type"] == "external_api"
    assert body["category"] == "productivity"
    # url은 config에 저장(마스킹 대상 아님)
    assert body["config"]["url"].endswith("swagger.json")

    listing = c.get("/paas/api/v1/modules", headers=ADMIN).json()
    assert any(m["name"] == "googleapis-com-calendar" for m in listing)


def test_import_dedupes_normalized_name(monkeypatch):
    _stub_directory(monkeypatch)
    c = TestClient(create_app())
    payload = {"name": "stripe.com", "url": "https://x", "category": None}
    first = c.post("/paas/api/v1/modules/import", json=payload, headers=ADMIN).json()
    second = c.post("/paas/api/v1/modules/import", json=payload, headers=ADMIN).json()
    assert first["name"] == "stripe-com"
    assert second["name"] == "stripe-com-2"  # 중복 시 접미사


def test_search_directory_failure_maps_to_502(monkeypatch):
    apisearch.clear_cache()
    httpx_retry.reset_breakers()

    def boom(url, **kw):
        raise httpx_retry.httpx.ConnectError("down")

    monkeypatch.setattr(httpx_retry.httpx, "get", boom)
    c = TestClient(create_app())
    r = c.get("/paas/api/v1/modules/search", params={"keyword": "x"}, headers=ADMIN)
    assert r.status_code == 502
