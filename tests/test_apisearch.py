"""외부 API 디렉터리 검색 — 필터링·정규화, /modules/search·/modules/import, admin 게이트.

소스는 둘이다: apis.guru(기본)와 공공데이터 카탈로그(PAAS_PUBLIC_DATA_URL, 기본 꺼짐).
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings

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
    hits = apisearch.search_apis("payment")["results"]
    assert [h["id"] for h in hits] == ["stripe.com"]
    # 카테고리로도 매칭
    assert apisearch.search_apis("productivity")["results"][0]["title"] == "Calendar API"
    assert apisearch.search_apis("nonexistent-kw")["results"] == []
    assert apisearch.search_apis("  ")["results"] == []


def test_search_filters_by_category(monkeypatch):
    _stub_directory(monkeypatch)
    assert [h["id"] for h in apisearch.search_apis("", "financial")["results"]] == ["stripe.com"]
    # 키워드와 AND — 둘 다 맞아야 걸린다
    assert apisearch.search_apis("payment", "financial")["results"][0]["id"] == "stripe.com"
    assert apisearch.search_apis("payment", "productivity")["results"] == []
    # 대소문자는 가리지 않는다
    assert apisearch.search_apis("", "FINANCIAL")["results"][0]["id"] == "stripe.com"


def test_uncategorized_entries_are_selectable(monkeypatch):
    """카테고리가 없는 항목은 카테고리 이름으로는 영영 걸리지 않는다 — 고를 값이 필요하다."""
    _stub_directory(monkeypatch)
    assert [h["id"] for h in apisearch.search_apis("", apisearch.UNCATEGORIZED)["results"]] == ["acme.io"]
    # "기타"를 골라도 키워드는 그대로 걸린다
    assert apisearch.search_apis("widget", apisearch.UNCATEGORIZED)["results"][0]["id"] == "acme.io"
    assert apisearch.search_apis("calendar", apisearch.UNCATEGORIZED)["results"] == []


def test_no_condition_returns_nothing(monkeypatch):
    """기본값은 전체지만, 아무 조건도 없으면 디렉터리를 통째로 쏟아내지 않는다."""
    _stub_directory(monkeypatch)
    assert apisearch.search_apis("", "")["results"] == []
    assert apisearch.search_apis("   ", "  ")["results"] == []


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


# --- 공공데이터 카탈로그 어댑터 ---

def _stub_public(monkeypatch, payload, status=200, guru=FAKE_DIRECTORY):
    """두 소스를 한 번에 세운다 — 주소로 갈라 서로 다른 응답을 준다."""
    apisearch.clear_cache()
    httpx_retry.reset_breakers()
    monkeypatch.setenv("PAAS_PUBLIC_DATA_URL", "https://catalog.example/api")
    get_settings.cache_clear()

    class _R:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

    def fake_get(url, **kw):
        if "catalog.example" in url:
            return _R(status, payload)
        return _R(200, guru)

    monkeypatch.setattr(httpx_retry.httpx, "get", fake_get)


def test_public_data_source_is_off_until_configured(monkeypatch):
    """설정하지 않은 설치본에 아웃바운드 호출을 새로 만들지 않는다."""
    _stub_directory(monkeypatch)
    monkeypatch.setenv("PAAS_PUBLIC_DATA_URL", "")
    get_settings.cache_clear()
    assert apisearch._public_data_items() == []


def test_public_data_items_merge_with_apisguru(monkeypatch):
    """두 소스의 결과가 한 목록으로 나온다."""
    _stub_public(monkeypatch, {"data": [
        {"apiNm": "기상청 단기예보 조회", "apiDesc": "동네예보 정보", "categoryNm": "공공행정",
         "linkUrl": "https://apis.data.go.kr/1360000/VilageFcstInfoService"},
    ]})
    body = apisearch.search_apis("예보")
    assert body["warnings"] == []
    assert [h["title"] for h in body["results"]] == ["기상청 단기예보 조회"]
    hit = body["results"][0]
    assert hit["provider"] == "공공데이터"
    assert hit["categories"] == ["공공행정"]
    assert hit["homepage"].endswith("VilageFcstInfoService")
    # 기존 소스도 그대로 나온다
    assert apisearch.search_apis("payment")["results"][0]["id"] == "stripe.com"


def test_public_data_list_can_be_nested_or_bare(monkeypatch):
    """카탈로그마다 감싸는 층이 다르다 — 목록만 찾으면 된다."""
    for payload in (
        [{"title": "대기오염정보"}],                                   # 그대로 리스트
        {"items": [{"title": "대기오염정보"}]},                        # 한 겹
        {"response": {"body": {"items": [{"title": "대기오염정보"}]}}},  # 두 겹
    ):
        _stub_public(monkeypatch, payload)
        assert apisearch.search_apis("대기")["results"][0]["title"] == "대기오염정보"


def test_unrecognised_shape_fails_loudly(monkeypatch):
    """형식이 어긋난 것과 '그런 데이터가 없다'가 구분되지 않으면 설정을 고칠 수 없다."""
    _stub_public(monkeypatch, {"resultCode": "99", "resultMsg": "SERVICE KEY IS NOT REGISTERED"})
    body = apisearch.search_apis("아무거나")
    # apis.guru는 살아 있으므로 결과는 나오고, 죽은 소스는 사유로 말한다
    assert body["warnings"] and "목록을 찾지 못했습니다" in body["warnings"][0]
    assert "resultCode" in body["warnings"][0]  # 무엇을 받았는지 그대로 보여 준다


def test_one_dead_source_does_not_kill_the_other(monkeypatch):
    """둘을 한 번에 실패시키면 '검색이 안 된다'만 남고 어느 쪽인지 알 수 없다."""
    _stub_public(monkeypatch, {"data": []}, status=500)
    body = apisearch.search_apis("payment")
    assert [h["id"] for h in body["results"]] == ["stripe.com"]
    assert body["warnings"] and "HTTP 500" in body["warnings"][0]


def test_both_sources_dead_is_an_error_not_an_empty_result(monkeypatch):
    """결과가 없는 것과 조회가 안 된 것은 다르다."""
    _stub_public(monkeypatch, {"data": []}, status=500, guru=None)
    apisearch.clear_cache()

    class _Bad:
        status_code = 500

        def json(self):
            return {}

    monkeypatch.setattr(httpx_retry.httpx, "get", lambda url, **kw: _Bad())
    with pytest.raises(apisearch.ApiSearchError):
        apisearch.search_apis("payment")
