"""정보 업데이트 — 등록·스캔·저장 제안·저장(새 저장소는 .env를 고쳐 바로 반영).

HTTP 경계(infosource._http_get)를 가짜 사이트로 바꾸고, 스캔 큐는 인라인으로 돌린다.
**헤더 값이 응답·감사 어디에도 나오지 않는지**를 함께 본다.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.services import bedrock, infosource, llm, storage

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"
SECRET = "SESSION=topsecret-cookie-value"

SITE = {
    "https://intra.test/": """<html><head><title>인사 포털</title></head><body>
        <nav><a href="/leave">휴가 조회</a><a href="/pay">급여 명세</a>
             <a href="/logout">로그아웃</a></nav>
        <h1>인사 포털</h1><a href="https://other.test/x">외부</a></body></html>""",
    "https://intra.test/leave": """<html><head><title>휴가 조회</title></head><body>
        <div class="lnb"><a href="/leave/history">사용 이력</a></div>
        <form><input name="year" placeholder="연도"><select name="type"></select></form>
        <table><tr><th>일자</th><th>종류</th><th>일수</th></tr></table></body></html>""",
    "https://intra.test/pay": "<html><head><title>급여 명세</title></head><body><h2>월별 급여</h2></body></html>",
    "https://intra.test/leave/history": "<html><head><title>사용 이력</title></head><body>이력</body></html>",
    "https://intra.test/robots.txt": "User-agent: *\nDisallow: /pay\n",
}


@pytest.fixture
def fake_site(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_get(url, headers, origin):
        calls.append((url, dict(headers)))
        if url not in SITE:
            return 404, url, "text/html", b""
        kind = "text/plain" if url.endswith(".txt") else "text/html; charset=utf-8"
        return 200, url, kind, SITE[url].encode("utf-8")

    monkeypatch.setattr(infosource, "_http_get", fake_get)
    monkeypatch.setattr(infosource, "browser_available", lambda: False)
    return calls


@pytest.fixture
def client(monkeypatch, tmp_path, fresh_settings, fake_site):
    root = tmp_path / "docs"
    root.mkdir()
    # 설정은 이 테스트의 .env에서 읽는다 — 저장이 그 파일을 고치고 다시 읽는 것까지 본다.
    env = tmp_path / ".env"
    env.write_text(f"# 운영 설정\nPAAS_DOC_ROOTS=docs={root}\nPAAS_TIER=small\n", encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "env_file", str(env))
    monkeypatch.delenv("PAAS_DOC_ROOTS", raising=False)
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setattr(llm, "default_provider", lambda db: None)
    monkeypatch.setattr(infosource, "_submit", infosource._scan_job)
    get_settings.cache_clear()
    c = TestClient(create_app())
    c.tmp = tmp_path
    return c


def _create(c, **over):
    body = {"name": "인사 포털", "kind": "web", "url": "https://intra.test/",
            "headers": f"Cookie: {SECRET}\nX-Team: hr", **over}
    res = c.post(f"{API}/sources", json=body, headers=ADMIN)
    assert res.status_code == 201, res.text
    return res.json()


def test_header_values_never_leave_the_server(client, fake_site):
    made = _create(client)
    assert made["headers"] == ["Cookie", "X-Team"]
    listed = client.get(f"{API}/sources", headers=ADMIN).text
    assert "topsecret" not in listed and "topsecret" not in json.dumps(made)

    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    detail = client.get(f"{API}/sources/{made['id']}", headers=ADMIN).json()
    assert detail["status"] == "scanned", detail["error"]
    assert "topsecret" not in json.dumps(detail)
    # 요청에는 실렸다 — 등록한 출처로 가는 것에만.
    assert any(h.get("Cookie") == SECRET for _, h in fake_site)

    from app.db import SessionLocal
    from app.models import AuditEvent

    with SessionLocal() as s:
        details = [json.dumps(e.detail, ensure_ascii=False) for e in s.query(AuditEvent).all()
                   if e.action.startswith("source.")]
    assert details and not any("topsecret" in d for d in details)


def test_web_scan_reads_menu_and_lookup_info(client, fake_site):
    made = _create(client)
    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    scan = client.get(f"{API}/sources/{made['id']}", headers=ADMIN).json()["scan"]
    urls = [p["url"] for p in scan["pages"]]
    assert urls[0] == "https://intra.test/"
    assert "https://intra.test/leave" in urls and "https://intra.test/leave/history" in urls
    # 로그아웃은 밟지 않고, robots.txt가 막은 곳과 다른 호스트는 받지 않는다.
    fetched = [u for u, _ in fake_site]
    assert "https://intra.test/logout" not in fetched
    assert "https://intra.test/pay" not in fetched
    assert not any(u.startswith("https://other.test") for u in fetched)

    leave = next(p for p in scan["pages"] if p["url"] == "https://intra.test/leave")
    assert leave["tables"] == [["일자", "종류", "일수"]]
    assert "연도" in leave["fields"]
    # LLM 없이도 메뉴가 나온다 — 시작 페이지 메뉴가 1단, 하위 페이지의 메뉴가 2단.
    top = {m["label"]: m for m in scan["menu"]}
    assert "휴가 조회" in top
    assert [c["label"] for c in top["휴가 조회"]["children"]] == ["사용 이력"]


def test_proposes_new_store_then_creates_it_via_env(client):
    made = _create(client, name="hr-portal")
    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    proposal = client.get(f"{API}/sources/{made['id']}", headers=ADMIN).json()["proposal"]
    assert proposal["mode"] == "new" and proposal["store"] == "hr-portal"
    # 제안 폴더는 기존 문서 폴더의 형제 자리다.
    assert proposal["path"] == str(client.tmp / "hr-portal")

    res = client.post(f"{API}/sources/{made['id']}/save", headers=ADMIN,
                      json={"mode": "new", "store": "hr-portal", "path": proposal["path"]})
    assert res.status_code == 200, res.text
    assert res.json()["created"] is True
    env = (client.tmp / ".env").read_text(encoding="utf-8")
    assert f"PAAS_DOC_ROOTS=docs={client.tmp / 'docs'},hr-portal={client.tmp / 'hr-portal'}" in env
    assert "# 운영 설정" in env and "PAAS_TIER=small" in env   # 다른 줄은 그대로
    assert (client.tmp / ".env.bak").exists()
    # 재시작 없이 보인다.
    assert storage.store("hr-portal") is not None
    index = (client.tmp / "hr-portal" / "hr-portal" / "index.md").read_text(encoding="utf-8")
    assert "## 메뉴 구성" in index and "휴가 조회" in index
    assert any(f.startswith("hr-portal/pages/") for f in res.json()["files"])


def test_save_into_existing_store(client):
    made = _create(client)
    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    res = client.post(f"{API}/sources/{made['id']}/save", headers=ADMIN,
                      json={"mode": "existing", "store": "docs"})
    assert res.status_code == 200, res.text
    assert (client.tmp / "docs" / "인사-포털" / "index.md").exists()


def test_new_store_is_refused_when_env_var_would_override(client, monkeypatch):
    made = _create(client)
    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    monkeypatch.setenv("PAAS_DOC_ROOTS", f"docs={client.tmp / 'docs'}")
    res = client.post(f"{API}/sources/{made['id']}/save", headers=ADMIN,
                      json={"mode": "new", "store": "hr", "path": str(client.tmp / "hr")})
    assert res.status_code == 400 and "환경변수" in res.json()["detail"]


def test_new_store_rejects_overlapping_folder(client):
    made = _create(client)
    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    res = client.post(f"{API}/sources/{made['id']}/save", headers=ADMIN,
                      json={"mode": "new", "store": "inner", "path": str(client.tmp / "docs" / "x")})
    assert res.status_code == 400 and "겹칩니다" in res.json()["detail"]


def test_llm_made_up_urls_are_dropped(client, monkeypatch):
    monkeypatch.setattr(llm, "default_provider", lambda db: object())
    monkeypatch.setattr(llm, "chat_completion", lambda provider, messages, db=None, **kw: json.dumps({
        "summary": "인사 정보 포털", "keywords": ["휴가"],
        "menu": [{"label": "휴가", "url": "https://intra.test/leave", "children": [
            {"label": "지어낸 메뉴", "url": "https://intra.test/made-up"}]}],
        "pages": [{"url": "https://intra.test/leave", "kind": "list", "info": "휴가 사용 내역"}],
        "store": {"mode": "existing", "name": "docs", "reason": "사내 문서"},
    }, ensure_ascii=False))
    made = _create(client)
    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    detail = client.get(f"{API}/sources/{made['id']}", headers=ADMIN).json()
    child = detail["scan"]["menu"][0]["children"][0]
    assert child["url"] == "" and child["label"] == "지어낸 메뉴"
    assert any("made-up" in n for n in detail["scan"]["notes"])
    assert detail["proposal"] == {**detail["proposal"], "mode": "existing", "store": "docs"}
    leave = next(p for p in detail["scan"]["pages"] if p["url"] == "https://intra.test/leave")
    assert leave["info"] == "휴가 사용 내역"


def test_rejects_secret_in_url_and_bad_scheme(client):
    for url in ("ftp://intra.test/", "https://intra.test/?access_token=abc"):
        res = client.post(f"{API}/sources", headers=ADMIN,
                          json={"name": "x", "kind": "web", "url": url})
        assert res.status_code == 400, url


def test_sso_cookie_is_refused(client):
    res = client.post(f"{API}/sources", headers=ADMIN, json={
        "name": "gps", "kind": "web", "url": "https://intra.test/",
        "headers": "Cookie: SESSION=a; SMSESSION=sso-value"})
    assert res.status_code == 400 and "SSO" in res.json()["detail"]
    assert "sso-value" not in res.text


def _browser_file(**over):
    payload = {
        "agent": "gpax-scan/1", "origin": "https://intra.test", "url": "https://intra.test/main",
        "menu": [{"label": "인사", "url": "", "children": [
            {"label": "휴가 조회", "url": "https://intra.test/leave", "children": []},
            {"label": "외부", "url": "https://other.test/x", "children": []}]}],
        "pages": [
            {"url": "https://intra.test/main", "title": "메인", "fields": ["사번"],
             "tables": [["일자", "종류"], "깨진 값"], "links": [
                 {"url": "https://intra.test/leave", "text": "휴가", "nav": True},
                 {"url": "https://other.test/x", "text": "외부"}]},
            {"url": "https://intra.test/main#휴가", "title": "휴가 탭", "tables": {"x": 1}},
            {"url": "https://other.test/x", "title": "남의 화면"},
        ],
        **over,
    }
    return {"file": ("gpax-scan-intra.test.json", json.dumps(payload).encode("utf-8"), "application/json")}


def test_browser_scan_result_is_checked_then_finished(client, monkeypatch):
    monkeypatch.setattr(infosource, "_submit_result", infosource._result_job)
    made = _create(client, headers="")
    url = f"{API}/sources/{made['id']}/browser-result"

    assert client.post(url, headers=ADMIN, files=_browser_file(agent="x")).status_code == 400
    res = client.post(url, headers=ADMIN, files=_browser_file(origin="https://other.test"))
    assert res.status_code == 400 and "다른 사이트" in res.json()["detail"]
    bad = {"file": ("a.json", b"not json", "application/json")}
    assert client.post(url, headers=ADMIN, files=bad).status_code == 400

    res = client.post(url, headers=ADMIN, files=_browser_file())
    assert res.status_code == 202, res.text
    detail = client.get(f"{API}/sources/{made['id']}", headers=ADMIN).json()
    assert detail["status"] == "scanned", detail["error"]
    scan = detail["scan"]
    assert scan["via"] == "browser"
    # 다른 출처의 화면·메뉴 주소는 버리고, 탭마다 붙인 #이름은 다른 화면으로 남는다.
    assert [p["url"] for p in scan["pages"]] == ["https://intra.test/main", "https://intra.test/main#휴가"]
    assert scan["pages"][0]["tables"] == [["일자", "종류"]]
    assert scan["pages"][1]["tables"] == []
    # 메뉴는 사용자 화면에 그려진 트리 그대로(LLM 없음).
    children = scan["menu"][0]["children"]
    assert [(m["label"], m["url"]) for m in children] == [
        ("휴가 조회", "https://intra.test/leave"), ("외부", "")]
    assert detail["proposal"]

    from app.db import SessionLocal
    from app.models import AuditEvent

    with SessionLocal() as s:
        assert s.query(AuditEvent).filter(AuditEvent.action == "source.browser_scan").count() == 1


def test_browser_scan_drops_cross_origin_links():
    from app.models import InfoSource

    payload = json.loads(_browser_file()["file"][1])
    result = infosource.from_browser(InfoSource(kind="web", url="https://intra.test/"), payload)
    assert [link["url"] for link in result["pages"][0]["links"]] == ["https://intra.test/leave"]


def test_browser_scan_only_for_web(client):
    made = _create(client, name="api", kind="api", url="https://intra.test/api", headers="")
    res = client.post(f"{API}/sources/{made['id']}/browser-result", headers=ADMIN,
                      files=_browser_file())
    assert res.status_code == 400


def test_api_source_reads_openapi(client, monkeypatch):
    spec = {"openapi": "3.0.0", "info": {"title": "재고 API"}, "paths": {
        "/items": {"get": {"summary": "품목 목록", "parameters": [{"name": "q"}],
                           "responses": {"200": {"content": {"application/json": {"schema": {
                               "type": "array", "items": {"$ref": "#/components/schemas/Item"}}}}}}},
                   "post": {"summary": "품목 등록"}}},
        "components": {"schemas": {"Item": {"properties": {"id": {}, "name": {}}}}}}

    def fake_get(url, headers, origin):
        if url == "https://api.test/openapi.json":
            return 200, url, "application/json", json.dumps(spec).encode()
        return 404, url, "text/html", b""

    monkeypatch.setattr(infosource, "_http_get", fake_get)
    made = _create(client, name="재고", kind="api", url="https://api.test/", headers="")
    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    scan = client.get(f"{API}/sources/{made['id']}", headers=ADMIN).json()["scan"]
    get_items = next(e for e in scan["endpoints"] if e["method"] == "GET")
    assert get_items["fields"] == ["id", "name"] and get_items["params"] == ["q"]
    assert [e["read_only"] for e in scan["endpoints"]] == [True, False]


def test_mcp_source_lists_tools(client, monkeypatch):
    sent = {}

    def fake_rpc(url, headers, payload):
        sent.update(headers)
        return {"result": {"tools": [
            {"name": "search_orders", "description": "주문 찾기", "inputSchema": {"properties": {"q": {}}}},
            {"name": "cancel_order", "description": "주문 취소"}]}}

    monkeypatch.setattr(infosource.mcp_client, "_post_rpc", fake_rpc)
    made = _create(client, name="주문", kind="mcp", url="https://mcp.test/mcp",
                   headers="Authorization: Bearer abc")
    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    scan = client.get(f"{API}/sources/{made['id']}", headers=ADMIN).json()["scan"]
    assert [(t["name"], t["read_only"]) for t in scan["tools"]] == [
        ("search_orders", True), ("cancel_order", False)]
    assert sent["Authorization"] == "Bearer abc"


def test_headers_are_not_sent_across_origins(monkeypatch):
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), request.headers.get("cookie")))
        if request.url.host == "intra.test":
            return httpx.Response(302, headers={"location": "https://sso.test/login"})
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    real = httpx.Client
    monkeypatch.setattr(infosource.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    status, final, _, _ = infosource._http_get("https://intra.test/", {"Cookie": SECRET},
                                                "https://intra.test")
    assert status == 200 and final == "https://sso.test/login"
    assert seen == [("https://intra.test/", SECRET), ("https://sso.test/login", None)]


def test_bedrock_carries_images():
    _, out = bedrock._to_converse([{"role": "user", "content": [
        {"type": "text", "text": "보라"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}}]}])
    assert out[0]["content"] == [
        {"text": "보라"}, {"image": {"format": "jpeg", "source": {"bytes": "QUJD"}}}]
