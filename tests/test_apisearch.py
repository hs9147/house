"""외부 API 카탈로그 — 수집(DB 적재)과 검색(DB 조회), /modules/search·/modules/import.

소스는 둘이다: apis.guru(기본)와 공공데이터 카탈로그(PAAS_PUBLIC_DATA_URL, 기본 꺼짐).
수집만 밖으로 나가고 검색은 표만 읽는다 — 그 성질을 여기서 확인한다.
"""
import copy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings

from app.main import create_app
from app.models import ApiCatalogEntry
from app.services import apisearch
from app.services import httpx_retry

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"

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


class _R:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def db():
    """표를 만들고(create_app) 세션 하나를 연다 — 검색은 이제 DB만 읽는다."""
    from app.db import SessionLocal

    create_app()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _stub_directory(monkeypatch, directory=None):
    """apis.guru 소스만 세운다(공공데이터는 꺼 둔다)."""
    httpx_retry.reset_breakers()
    monkeypatch.setenv("PAAS_PUBLIC_DATA_URL", "")
    get_settings.cache_clear()
    body = FAKE_DIRECTORY if directory is None else directory
    monkeypatch.setattr(httpx_retry.httpx, "get", lambda url, **kw: _R(200, body))


def _synced(monkeypatch, db, directory=None):
    _stub_directory(monkeypatch, directory)
    return apisearch.sync_catalog(db)


def _row(db, ext_id: str) -> ApiCatalogEntry:
    db.expire_all()
    return db.execute(
        select(ApiCatalogEntry).where(ApiCatalogEntry.ext_id == ext_id)
    ).scalar_one()


def test_normalize_module_name():
    assert apisearch.normalize_module_name("googleapis.com:calendar") == "googleapis-com-calendar"
    assert apisearch.normalize_module_name("Stripe API!!") == "stripe-api"
    assert apisearch.normalize_module_name(":::") == "api"


# --- 검색(DB만 읽는다) ---

def test_search_filters_by_keyword(monkeypatch, db):
    _synced(monkeypatch, db)
    hits = apisearch.search_apis(db, "payment")["results"]
    assert [h["id"] for h in hits] == ["stripe.com"]
    # 카테고리로도 매칭
    assert apisearch.search_apis(db, "productivity")["results"][0]["title"] == "Calendar API"
    assert apisearch.search_apis(db, "nonexistent-kw")["results"] == []
    assert apisearch.search_apis(db, "  ")["results"] == []


def test_search_filters_by_category(monkeypatch, db):
    _synced(monkeypatch, db)
    assert [h["id"] for h in apisearch.search_apis(db, "", "financial")["results"]] == ["stripe.com"]
    # 키워드와 AND — 둘 다 맞아야 걸린다
    assert apisearch.search_apis(db, "payment", "financial")["results"][0]["id"] == "stripe.com"
    assert apisearch.search_apis(db, "payment", "productivity")["results"] == []
    # 대소문자는 가리지 않는다
    assert apisearch.search_apis(db, "", "FINANCIAL")["results"][0]["id"] == "stripe.com"


def test_uncategorized_entries_are_selectable(monkeypatch, db):
    """카테고리가 없는 항목은 카테고리 이름으로는 영영 걸리지 않는다 — 고를 값이 필요하다."""
    _synced(monkeypatch, db)
    hits = apisearch.search_apis(db, "", apisearch.UNCATEGORIZED)["results"]
    assert [h["id"] for h in hits] == ["acme.io"]
    # "기타"를 골라도 키워드는 그대로 걸린다
    assert apisearch.search_apis(db, "widget", apisearch.UNCATEGORIZED)["results"][0]["id"] == "acme.io"
    assert apisearch.search_apis(db, "calendar", apisearch.UNCATEGORIZED)["results"] == []


def test_no_condition_returns_nothing(monkeypatch, db):
    """기본값은 전체지만, 아무 조건도 없으면 카탈로그를 통째로 쏟아내지 않는다."""
    _synced(monkeypatch, db)
    assert apisearch.search_apis(db, "", "")["results"] == []
    assert apisearch.search_apis(db, "   ", "  ")["results"] == []


def test_category_list_comes_from_the_catalog(monkeypatch, db):
    _synced(monkeypatch, db)
    assert apisearch.list_categories(db) == [
        {"name": "financial", "count": 1},
        {"name": "productivity", "count": 1},
        {"name": "기타", "count": 1},
    ]


def test_search_reads_the_db_without_going_outbound(monkeypatch, db):
    """**이 성질이 /mcp/apis를 관리자 키 없이 열 수 있는 근거다.**

    예전에는 검색이 곧 디렉터리 조회라 admin으로 묶을 수밖에 없었다.
    """
    _synced(monkeypatch, db)

    def boom(url, **kw):
        raise AssertionError(f"검색이 밖으로 나갔다: {url}")

    monkeypatch.setattr(httpx_retry.httpx, "get", boom)
    assert apisearch.search_apis(db, "payment")["results"][0]["id"] == "stripe.com"
    assert apisearch.list_categories(db)
    assert apisearch.catalog_status(db)["total"] == 3


def test_empty_catalog_is_not_the_same_as_no_match(db):
    """결과가 없는 것과 아직 아무 것도 안 받은 것은 다른 문제다."""
    body = apisearch.search_apis(db, "payment")
    assert body["results"] == []
    assert body["warnings"] == [apisearch.EMPTY_CATALOG]


# --- 수집(바뀐 것만 쓴다) ---

def test_sync_writes_only_what_changed(monkeypatch, db):
    first = _synced(monkeypatch, db)
    assert (first["added"], first["updated"], first["unchanged"]) == (3, 0, 0)

    stamp = _row(db, "stripe.com").updated_at
    second = _synced(monkeypatch, db)
    assert (second["added"], second["updated"], second["unchanged"]) == (0, 0, 3)
    # 안 바뀐 행에는 UPDATE 자체가 나가지 않는다 — "언제 바뀌었나"가 "언제 받았나"에
    # 덮이면 무엇이 실제로 달라졌는지 알 수 없게 된다.
    assert _row(db, "stripe.com").updated_at == stamp

    renamed = copy.deepcopy(FAKE_DIRECTORY)
    renamed["stripe.com"]["versions"]["1.0"]["info"]["title"] = "Stripe Payments"
    third = _synced(monkeypatch, db, renamed)
    assert (third["added"], third["updated"], third["unchanged"]) == (0, 1, 2)
    row = _row(db, "stripe.com")
    assert row.title == "Stripe Payments"
    assert row.updated_at > stamp
    # 건초더미도 함께 따라간다 — 아니면 새 이름으로는 검색되지 않는다
    assert apisearch.search_apis(db, "stripe payments")["results"][0]["id"] == "stripe.com"


def test_missing_entries_are_marked_removed_not_deleted(monkeypatch, db):
    _synced(monkeypatch, db)

    shrunk = {k: v for k, v in copy.deepcopy(FAKE_DIRECTORY).items() if k != "stripe.com"}
    assert _synced(monkeypatch, db, shrunk)["removed"] == 1
    assert apisearch.search_apis(db, "payment")["results"] == []
    # 행은 남는다 — 잠깐 빠졌다 돌아오는 일이 흔하고, 지우면 그 사이 기록도 사라진다
    assert _row(db, "stripe.com").removed_at is not None
    status = apisearch.catalog_status(db)["sources"]["apisguru"]
    assert (status["total"], status["removed"]) == (2, 1)

    assert _synced(monkeypatch, db)["restored"] == 1
    assert apisearch.search_apis(db, "payment")["results"][0]["id"] == "stripe.com"
    assert _row(db, "stripe.com").removed_at is None


def test_empty_response_does_not_wipe_the_catalog(monkeypatch, db):
    """빈 응답은 "카탈로그가 통째로 사라졌다"보다 "응답이 깨졌다"가 훨씬 그럴듯하다."""
    _synced(monkeypatch, db)
    assert _synced(monkeypatch, db, {})["removed"] == 0
    assert apisearch.search_apis(db, "payment")["results"][0]["id"] == "stripe.com"


def test_failed_source_keeps_its_rows(monkeypatch, db):
    """조회 실패를 "없어졌다"로 기록하면 멀쩡한 카탈로그가 통째로 사라진다."""
    _synced(monkeypatch, db)
    monkeypatch.setattr(httpx_retry.httpx, "get", lambda url, **kw: _R(500, {}))
    with pytest.raises(apisearch.ApiSearchError):
        apisearch.sync_catalog(db)
    assert apisearch.search_apis(db, "payment")["results"][0]["id"] == "stripe.com"


# --- 엔드포인트 ---

def test_category_endpoints(monkeypatch, db):
    _stub_directory(monkeypatch)
    c = TestClient(create_app())
    assert c.post(f"{API}/modules/search/refresh", headers=ADMIN).json()["added"] == 3

    body = c.get(f"{API}/modules/search/categories", headers=ADMIN).json()
    assert body["uncategorized_label"] == "기타"
    assert [i["name"] for i in body["categories"]] == ["financial", "productivity", "기타"]

    hits = c.get(f"{API}/modules/search",
                 params={"category": "financial"}, headers=ADMIN).json()["results"]
    assert [h["id"] for h in hits] == ["stripe.com"]
    # 카테고리만으로도 검색된다(keyword 없이)
    assert c.get(f"{API}/modules/search",
                 params={"category": "기타"}, headers=ADMIN).json()["results"][0]["id"] == "acme.io"


def test_search_endpoint_admin_only(monkeypatch, db):
    _stub_directory(monkeypatch)
    c = TestClient(create_app())
    c.post(f"{API}/modules/search/refresh", headers=ADMIN)
    r = c.get(f"{API}/modules/search", params={"keyword": "payment"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["results"][0]["id"] == "stripe.com"

    member = c.post(f"{API}/keys", json={"name": "dev"}, headers=ADMIN).json()["key"]
    r2 = c.get(f"{API}/modules/search", params={"keyword": "x"},
               headers={"x-api-key": member})
    assert r2.status_code == 403


def test_refresh_failure_maps_to_502(monkeypatch):
    httpx_retry.reset_breakers()
    monkeypatch.setenv("PAAS_PUBLIC_DATA_URL", "")
    get_settings.cache_clear()

    def boom(url, **kw):
        raise httpx_retry.httpx.ConnectError("down")

    monkeypatch.setattr(httpx_retry.httpx, "get", boom)
    c = TestClient(create_app())
    assert c.post(f"{API}/modules/search/refresh", headers=ADMIN).status_code == 502
    # 검색은 밖으로 나가지 않으므로 502가 아니라 "비어 있다"로 답한다
    r = c.get(f"{API}/modules/search", params={"keyword": "x"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["warnings"] == [apisearch.EMPTY_CATALOG]


def test_import_creates_external_api_module(monkeypatch, db):
    _stub_directory(monkeypatch)
    c = TestClient(create_app())
    r = c.post(f"{API}/modules/import", json={
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

    listing = c.get(f"{API}/modules", headers=ADMIN).json()
    assert any(m["name"] == "googleapis-com-calendar" for m in listing)


def test_import_dedupes_normalized_name(monkeypatch, db):
    _stub_directory(monkeypatch)
    c = TestClient(create_app())
    payload = {"name": "stripe.com", "url": "https://x", "category": None}
    first = c.post(f"{API}/modules/import", json=payload, headers=ADMIN).json()
    second = c.post(f"{API}/modules/import", json=payload, headers=ADMIN).json()
    assert first["name"] == "stripe-com"
    assert second["name"] == "stripe-com-2"  # 중복 시 접미사


# --- 공공데이터 카탈로그 어댑터 ---

def _stub_public(monkeypatch, payload, status=200, guru=FAKE_DIRECTORY):
    """두 소스를 한 번에 세운다 — 주소로 갈라 서로 다른 응답을 준다."""
    httpx_retry.reset_breakers()
    monkeypatch.setenv("PAAS_PUBLIC_DATA_URL", "https://catalog.example/api")
    get_settings.cache_clear()

    def fake_get(url, **kw):
        if "catalog.example" in url:
            return _R(status, payload)
        return _R(200, guru)

    monkeypatch.setattr(httpx_retry.httpx, "get", fake_get)


def test_public_data_source_is_off_until_configured(monkeypatch, db):
    """설정하지 않은 설치본에 아웃바운드 호출을 새로 만들지 않는다.

    그래도 소스 자체는 현황에 남는다 — 빼 버리면 "공공데이터가 왜 안 나오지"에 답할
    자리가 없어진다. enabled=false가 "주소를 안 넣어 아예 안 부른다"는 답이다.
    """
    _synced(monkeypatch, db)
    assert apisearch._public_data_items() == []

    sources = apisearch.catalog_status(db)["sources"]
    assert sources["apisguru"]["enabled"] is True
    assert sources["publicdata"] == {
        "label": "공공데이터", "enabled": False, "total": 0, "removed": 0, "updated_at": None}


def test_public_data_items_merge_with_apisguru(monkeypatch, db):
    """두 소스의 결과가 한 목록으로 나온다."""
    _stub_public(monkeypatch, {"data": [
        {"apiNm": "기상청 단기예보 조회", "apiDesc": "동네예보 정보", "categoryNm": "공공행정",
         "linkUrl": "https://apis.data.go.kr/1360000/VilageFcstInfoService"},
    ]})
    assert apisearch.sync_catalog(db)["warnings"] == []

    body = apisearch.search_apis(db, "예보")
    assert body["warnings"] == []
    assert [h["title"] for h in body["results"]] == ["기상청 단기예보 조회"]
    hit = body["results"][0]
    assert hit["provider"] == "공공데이터"
    assert hit["categories"] == ["공공행정"]
    assert hit["homepage"].endswith("VilageFcstInfoService")
    assert hit["source"] == apisearch.SOURCE_PUBLIC_DATA
    # 기존 소스도 그대로 나온다
    assert apisearch.search_apis(db, "payment")["results"][0]["id"] == "stripe.com"


def test_public_data_list_can_be_nested_or_bare(monkeypatch, db):
    """카탈로그마다 감싸는 층이 다르다 — 목록만 찾으면 된다."""
    for payload in (
        [{"title": "대기오염정보"}],                                   # 그대로 리스트
        {"items": [{"title": "대기오염정보"}]},                        # 한 겹
        {"response": {"body": {"items": [{"title": "대기오염정보"}]}}},  # 두 겹
    ):
        _stub_public(monkeypatch, payload)
        apisearch.sync_catalog(db)
        assert apisearch.search_apis(db, "대기")["results"][0]["title"] == "대기오염정보"


def test_unrecognised_shape_fails_loudly(monkeypatch, db):
    """형식이 어긋난 것과 '그런 데이터가 없다'가 구분되지 않으면 설정을 고칠 수 없다."""
    _stub_public(monkeypatch, {"resultCode": "99", "resultMsg": "SERVICE KEY IS NOT REGISTERED"})
    stats = apisearch.sync_catalog(db)
    # apis.guru는 살아 있으므로 수집은 되고, 죽은 소스는 사유로 말한다
    assert stats["sources"] == [apisearch.SOURCE_APISGURU]
    assert stats["warnings"] and "목록을 찾지 못했습니다" in stats["warnings"][0]
    assert "resultCode" in stats["warnings"][0]  # 무엇을 받았는지 그대로 보여 준다


def test_one_dead_source_does_not_kill_the_other(monkeypatch, db):
    """둘을 한 번에 실패시키면 '수집이 안 된다'만 남고 어느 쪽인지 알 수 없다."""
    _stub_public(monkeypatch, {"data": []}, status=500)
    stats = apisearch.sync_catalog(db)
    assert stats["added"] == 3
    assert stats["warnings"] and "HTTP 500" in stats["warnings"][0]
    assert [h["id"] for h in apisearch.search_apis(db, "payment")["results"]] == ["stripe.com"]


def test_both_sources_dead_is_an_error_not_an_empty_result(monkeypatch, db):
    """받은 것이 없는 것과 바뀐 것이 없는 것은 다르다."""
    _stub_public(monkeypatch, {"data": []}, status=500)
    monkeypatch.setattr(httpx_retry.httpx, "get", lambda url, **kw: _R(500, {}))
    with pytest.raises(apisearch.ApiSearchError):
        apisearch.sync_catalog(db)


# --- 카테고리 자리에 문자열이 오는 경우 ---

def test_a_category_given_as_a_string_is_not_split_into_letters(monkeypatch, db):
    """소스가 카테고리 자리에 리스트가 아니라 문자열 하나를 넣어 주는 일이 있다.

    그대로 두면 리스트처럼 순회되는 곳마다 글자 단위로 쪼개진다 — 카테고리 목록에
    s·e·c·u·r·i·t·y가 따로 서고, 검색용 건초더미도 "s e c u r i t y"가 된다.
    """
    directory = {"acme.io": {"preferred": "1.0", "versions": {"1.0": {
        "info": {"title": "Acme", "description": "auth 게이트웨이",
                 "x-apisguru-categories": "security"},
        "swaggerUrl": "https://example.test/s.json"}}}}
    _synced(monkeypatch, db, directory)

    assert apisearch.list_categories(db) == [{"name": "security", "count": 1}]
    assert _row(db, "acme.io").categories == ["security"]
    # 카테고리로 고를 수 있어야 하고
    assert [h["id"] for h in apisearch.search_apis(db, "", "security")["results"]] == ["acme.io"]
    # 건초더미가 쪼개졌으면 낱말로는 안 걸린다
    assert apisearch.search_apis(db, "security")["results"][0]["id"] == "acme.io"
    # 카테고리가 있는 항목이므로 "기타"에는 안 걸린다
    assert apisearch.search_apis(db, "", apisearch.UNCATEGORIZED)["results"] == []


def test_a_string_already_in_the_table_still_reads_as_one_category(db):
    """고치기 전에 들어간 행이 이미 있다 — 다시 수집하기 전에도 화면이 맞아야 한다."""
    db.add(ApiCatalogEntry(
        source=apisearch.SOURCE_APISGURU, ext_id="acme.io", title="Acme",
        description="auth", provider="acme.io", categories="security",
        homepage="", spec_url="", search_text="acme.io acme auth security",
    ))
    db.commit()
    assert apisearch.list_categories(db) == [{"name": "security", "count": 1}]
    hit = apisearch.search_apis(db, "", "security")["results"][0]
    assert hit["categories"] == ["security"]



# --- 소스로 좁히기 ---

def test_search_can_narrow_to_one_source(monkeypatch, db):
    """공공데이터만 보고 싶을 때가 있다 — 합쳐 놓기만 하면 고를 방법이 없다."""
    _stub_public(monkeypatch, {"data": [
        {"apiNm": "기상청 단기예보 조회", "apiDesc": "동네예보 정보", "categoryNm": "공공행정"},
    ]})
    apisearch.sync_catalog(db)

    only_public = apisearch.search_apis(db, "", "", apisearch.SOURCE_PUBLIC_DATA)["results"]
    assert [h["title"] for h in only_public] == ["기상청 단기예보 조회"]
    assert {h["source"] for h in only_public} == {apisearch.SOURCE_PUBLIC_DATA}

    # 소스만 골라도 조건이다 — 키워드 없이 그 소스를 훑는다
    only_guru = apisearch.search_apis(db, "", "", apisearch.SOURCE_APISGURU)["results"]
    assert {h["source"] for h in only_guru} == {apisearch.SOURCE_APISGURU}
    assert len(only_guru) == 3

    # 키워드·카테고리와는 AND로 걸린다
    assert apisearch.search_apis(
        db, "payment", "", apisearch.SOURCE_PUBLIC_DATA)["results"] == []
    assert apisearch.search_apis(
        db, "payment", "", apisearch.SOURCE_APISGURU)["results"][0]["id"] == "stripe.com"

    # 소스도 조건이 아니었다면 셋 다 빈 것과 같아 빈 목록이 나온다
    assert apisearch.search_apis(db, "", "", "")["results"] == []


def test_an_unknown_source_fails_instead_of_answering_empty(monkeypatch, db):
    """라벨을 키 자리에 넣는 실수가 "그런 API가 없다"로 보이면 원인을 찾을 수 없다."""
    _synced(monkeypatch, db)
    with pytest.raises(apisearch.ApiSearchError) as e:
        apisearch.search_apis(db, "", "", "공공데이터")   # 라벨이지 키가 아니다
    assert "publicdata" in str(e.value)   # 쓸 수 있는 값을 함께 보여 준다


def test_search_endpoint_takes_a_source_and_rejects_an_unknown_one(monkeypatch, db):
    _stub_directory(monkeypatch)
    c = TestClient(create_app())
    c.post(f"{API}/modules/search/refresh", headers=ADMIN)

    hits = c.get(f"{API}/modules/search",
                 params={"source": apisearch.SOURCE_APISGURU}, headers=ADMIN).json()["results"]
    assert len(hits) == 3

    bad = c.get(f"{API}/modules/search", params={"source": "nope"}, headers=ADMIN)
    assert bad.status_code == 400   # 상류 장애(502)가 아니라 인자가 틀린 것이다
    assert "nope" in bad.json()["detail"]


def test_status_endpoint_shows_every_source_even_at_zero(monkeypatch, db):
    _stub_directory(monkeypatch)
    c = TestClient(create_app())
    c.post(f"{API}/modules/search/refresh", headers=ADMIN)

    body = c.get(f"{API}/modules/search/status", headers=ADMIN).json()
    assert body["total"] == 3
    assert body["sources"]["apisguru"]["total"] == 3
    assert body["sources"]["apisguru"]["label"] == "apis.guru"
    # 한 번도 못 받은 소스도 나온다 — 화면에서 고를 수 있어야 이유를 확인할 수 있다
    assert body["sources"]["publicdata"] == {
        "label": "공공데이터", "enabled": False, "total": 0, "removed": 0, "updated_at": None}


# --- 주소로 찾기 ---

def test_search_finds_an_api_by_its_url(monkeypatch, db):
    """받아 놓은 주소를 되짚는 일이 잦다 — 이름·설명만 훑으면 그 주소로는 영영 안 걸린다."""
    _synced(monkeypatch, db)
    # 홈페이지(contact.url)
    assert apisearch.search_apis(db, "stripe.com")["results"][0]["id"] == "stripe.com"
    # 스펙 주소 — 이름·설명 어디에도 없는 문자열이다
    spec = "api.apis.guru/v2/specs/google/calendar/v3/swagger.json"
    assert apisearch.search_apis(db, spec)["results"][0]["title"] == "Calendar API"


def test_a_pasted_url_matches_despite_scheme_and_trailing_slash(monkeypatch, db):
    """브라우저에서 복사하면 https://가 붙고 끝에 슬래시가 붙는다 — 한 글자만 어긋나도
    부분일치가 깨진다."""
    _synced(monkeypatch, db)
    for pasted in (
        "https://stripe.com",
        "https://stripe.com/",
        "http://stripe.com",
        "STRIPE.COM",
    ):
        hits = apisearch.search_apis(db, pasted)["results"]
        assert [h["id"] for h in hits] == ["stripe.com"], pasted


def test_sync_rebuilds_a_stale_haystack(monkeypatch, db):
    """건초더미 만드는 방법이 바뀌면 이미 쌓인 행도 따라와야 한다.

    URL을 넣기 전에 수집한 행은 필드가 그대로라 "안 바뀜"으로 지나가고, 주소로는
    영영 안 걸린 채 남는다.
    """
    spec = "api.apis.guru/v2/specs/google/calendar/v3/swagger.json"
    _synced(monkeypatch, db)
    row = _row(db, "googleapis.com:calendar")
    # 주소가 빠진 옛 방식 — 이름·설명·카테고리만 들어 있다
    row.search_text = "googleapis.com:calendar calendar api manipulates events productivity"
    db.commit()
    assert apisearch.search_apis(db, spec)["results"] == []

    stats = _synced(monkeypatch, db)
    assert (stats["updated"], stats["unchanged"]) == (1, 2)
    assert apisearch.search_apis(db, spec)["results"][0]["title"] == "Calendar API"
