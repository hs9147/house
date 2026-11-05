"""웹 검색(services/websearch · MCP /mcp/web) — 출처마다 강점을 글로 주고 LLM이 고른다.

밖으로 나가는 호출은 가짜(httpx)로 바꾼다. 보는 것: 키를 넣은 출처만 켜지는지, 출처마다 요청 모양(Brave 일본은
나라·언어 조건과 구독 토큰, Naver는 헤더, Baidu는 Bearer), 결과에서 태그를 걷어 내는지, 꺼진·모르는 출처, 한 시간 상한, 감사에
검색어가 남지 않는지, 설정했을 때만 서버 목록에 나오는지."""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import websearch

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


class _R:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body


@pytest.fixture
def web(monkeypatch, fresh_settings):
    for k, v in {"PAAS_SEARCH_BRAVE_KEY": "bt", "PAAS_SEARCH_NAVER_ID": "nid",
                 "PAAS_SEARCH_NAVER_SECRET": "nsec", "PAAS_SEARCH_BAIDU_KEY": "",
                 "PAAS_SEARCH_APPROVED": "global,naver,naver_news,japan,baidu"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    websearch._used.clear()
    calls = []

    def get(url, params=None, headers=None, timeout=None):
        calls.append(("GET", url, params, headers))
        if "brave.com" in url:
            return _R(200, {"web": {"results": [{"title": "Spec <strong>A</strong>", "url": "https://a.example/",
                                                 "description": "about &amp; A", "age": "2 days ago"}]}})
        return _R(200, {"items": [{"title": "<b>배터리</b> 협력사", "link": "https://n.example/1",
                                   "originallink": "https://press.example/1", "description": "국내 &quot;소식&quot;",
                                   "pubDate": "Wed, 05 Nov 2026 09:00:00 +0900"}]})

    def post(url, json=None, headers=None, timeout=None):
        calls.append(("POST", url, json, headers))
        return _R(200, {"references": [{"title": "百度 结果", "url": "https://b.example/", "content": "内容"}]})

    monkeypatch.setattr(websearch.httpx, "get", get)
    monkeypatch.setattr(websearch.httpx, "post", post)
    return calls


def test_sources_describe_their_strengths_and_only_keyed_ones_are_on(web):
    by = {s["id"]: s for s in websearch.sources()}
    assert list(by) == ["global", "naver", "naver_news", "japan", "baidu"]
    assert all(s["strengths"] for s in by.values())            # LLM이 읽고 고르는 글
    assert "한국" in by["naver"]["strengths"] and "중국" in by["baidu"]["strengths"]
    assert "일본" in by["japan"]["strengths"] and "야후 재팬" in by["japan"]["strengths"]
    assert {k for k, s in by.items() if s["enabled"]} == {"global", "naver", "naver_news", "japan"}


def test_each_source_is_asked_its_own_way_and_results_are_cleaned(web, monkeypatch):
    out = websearch.search("a@lge.com", "battery spec", "japan", 3)
    method, url, params, headers = web[-1]
    assert (params["country"], params["search_lang"], params["count"]) == ("JP", "jp", 3)
    assert headers["X-Subscription-Token"] == "bt"
    assert out["results"] == [{"title": "Spec A", "url": "https://a.example/", "snippet": "about & A",
                               "date": "2 days ago", "source": "japan"}]

    out = websearch.search("a@lge.com", "배터리 협력사", "naver_news")
    method, url, params, headers = web[-1]
    assert url.endswith("/news.json") and headers["X-Naver-Client-Id"] == "nid"
    hit = out["results"][0]
    assert (hit["title"], hit["url"], hit["snippet"]) == ("배터리 협력사", "https://press.example/1", '국내 "소식"')
    assert hit["date"].startswith("Wed")

    monkeypatch.setenv("PAAS_SEARCH_BAIDU_KEY", "bk")
    get_settings.cache_clear()
    out = websearch.search("a@lge.com", "电池 供应商", "baidu")
    method, url, body, headers = web[-1]
    assert method == "POST" and headers["Authorization"] == "Bearer bk" and body["messages"][0]["content"] == "电池 供应商"
    assert out["results"][0]["url"] == "https://b.example/"


def test_off_unknown_empty_and_hourly_limit(web, monkeypatch):
    with pytest.raises(websearch.SearchError, match="꺼져 있습니다"):
        websearch.search("a@lge.com", "x", "baidu")
    with pytest.raises(websearch.SearchError, match="모르는 출처"):
        websearch.search("a@lge.com", "x", "bing")
    with pytest.raises(websearch.SearchError, match="비어 있습니다"):
        websearch.search("a@lge.com", "  ", "global")
    monkeypatch.setenv("PAAS_SEARCH_MAX_PER_HOUR", "2")
    get_settings.cache_clear()
    websearch.search("a@lge.com", "x", "global")
    websearch.search("a@lge.com", "x", "global")
    with pytest.raises(websearch.SearchError, match="한 시간에 2번"):
        websearch.search("a@lge.com", "x", "global")
    websearch.search("b@lge.com", "x", "global")                # 부르는 사람마다 따로


def test_engine_errors_say_what_the_engine_said(web, monkeypatch):
    monkeypatch.setattr(websearch.httpx, "get", lambda *a, **k: _R(422, {"error": {"detail": "Unable to validate"}}))
    with pytest.raises(websearch.SearchError, match="HTTP 422.*Unable to validate"):
        websearch.search("a@lge.com", "x", "global")


def test_mcp_web_server_and_audit_keep_the_query_out(web):
    c = TestClient(create_app())

    def call(tool, args=None):
        return c.post(f"{API}/mcp/web", headers=ADMIN, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args or {}}}).json()

    assert '"strengths"' in call("list_search_sources")["result"]["content"][0]["text"]
    ok = call("web_search", {"query": "비밀-검색어 배터리", "source": "naver"})
    assert "press.example" in ok["result"]["content"][0]["text"]
    assert "꺼져 있습니다" in call("web_search", {"query": "x", "source": "baidu"})["error"]["message"]

    from app.db import SessionLocal
    from app.models import AuditEvent
    with SessionLocal() as s:
        event = s.query(AuditEvent).filter(AuditEvent.action == "mcp.web.search").one()
        assert event.target == "naver" and event.detail["results"] == 1
        assert "비밀-검색어" not in str(event.detail)


def test_web_server_is_listed_only_when_a_source_is_keyed(monkeypatch, fresh_settings):
    c = TestClient(create_app())
    ids = lambda: [s["id"] for s in c.get(f"{API}/mcp/search", headers=ADMIN).json()]  # noqa: E731
    for k in ("PAAS_SEARCH_BRAVE_KEY", "PAAS_SEARCH_NAVER_ID", "PAAS_SEARCH_NAVER_SECRET", "PAAS_SEARCH_BAIDU_KEY"):
        monkeypatch.setenv(k, "")
    get_settings.cache_clear()
    assert "paas-web" not in ids()
    monkeypatch.setenv("PAAS_SEARCH_NAVER_ID", "nid")
    monkeypatch.setenv("PAAS_SEARCH_NAVER_SECRET", "nsec")
    monkeypatch.setenv("PAAS_SEARCH_APPROVED", "")
    get_settings.cache_clear()
    assert "paas-web" not in ids()                      # 키가 있어도 검증 전이면 올리지 않는다
    monkeypatch.setenv("PAAS_SEARCH_APPROVED", "naver")
    get_settings.cache_clear()
    assert "paas-web" in ids()


def test_keyed_sources_stay_off_until_security_and_cost_are_verified(web, monkeypatch):
    """보안·비용 검증을 마친 출처(PAAS_SEARCH_APPROVED)만 운영에 들어간다 — 키가 있어도 그 전에는 쓰지 않는다."""
    monkeypatch.setenv("PAAS_SEARCH_APPROVED", "naver")
    get_settings.cache_clear()
    by = {s["id"]: s for s in websearch.sources()}
    assert {k: s["state"] for k, s in by.items()} == {
        "global": "pending", "naver": "on", "naver_news": "pending", "japan": "pending", "baidu": "no_key"}
    assert [k for k, s in by.items() if s["enabled"]] == ["naver"]
    before = len(web)
    with pytest.raises(websearch.SearchError, match="보안·비용 검증 전"):
        websearch.search("a@lge.com", "x", "global")
    assert len(web) == before                                  # 밖으로 나가지도 않는다
    websearch.search("a@lge.com", "배터리", "naver")
