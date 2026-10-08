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

    def fake_get(url, headers, origin, **kw):
        calls.append((url, dict(headers)))
        if url not in SITE:
            return 404, url, "text/html", b""
        body = SITE[url]
        if isinstance(body, tuple):   # (content-type, bytes) — 내려받기 파일
            return 200, url, body[0], body[1]
        kind = "text/plain" if url.endswith(".txt") else "text/html; charset=utf-8"
        return 200, url, kind, body.encode("utf-8")

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


def _save(c, source_id, **targets):
    return c.post(f"{API}/sources/{source_id}/save", headers=ADMIN, json={"targets": targets})


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
    proposal = client.get(f"{API}/sources/{made['id']}", headers=ADMIN).json()["proposal"]["targets"]
    assert list(proposal) == ["pages"]   # 받은 파일이 없으면 정리만 저장한다
    assert proposal["pages"]["mode"] == "new" and proposal["pages"]["store"] == "hr-portal"
    # 제안 폴더는 기존 문서 폴더의 형제 자리다.
    assert proposal["pages"]["path"] == str(client.tmp / "hr-portal")

    res = _save(client, made["id"], pages={"mode": "new", "store": "hr-portal",
                                           "path": proposal["pages"]["path"]})
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
    res = _save(client, made["id"], pages={"mode": "existing", "store": "docs"})
    assert res.status_code == 200, res.text
    assert (client.tmp / "docs" / "인사-포털" / "index.md").exists()


def test_new_store_is_refused_when_env_var_would_override(client, monkeypatch):
    made = _create(client)
    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    monkeypatch.setenv("PAAS_DOC_ROOTS", f"docs={client.tmp / 'docs'}")
    res = _save(client, made["id"], pages={"mode": "new", "store": "hr", "path": str(client.tmp / "hr")})
    assert res.status_code == 400 and "환경변수" in res.json()["detail"]


def test_new_store_rejects_overlapping_folder(client):
    made = _create(client)
    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    res = _save(client, made["id"], pages={"mode": "new", "store": "inner",
                                           "path": str(client.tmp / "docs" / "x")})
    assert res.status_code == 400 and "겹칩니다" in res.json()["detail"]


def test_llm_made_up_urls_are_dropped(client, monkeypatch):
    monkeypatch.setattr(llm, "default_provider", lambda db: object())
    monkeypatch.setattr(llm, "chat_completion", lambda provider, messages, db=None, **kw: json.dumps({
        "summary": "인사 정보 포털", "keywords": ["휴가"],
        "menu": [{"label": "휴가", "url": "https://intra.test/leave", "children": [
            {"label": "지어낸 메뉴", "url": "https://intra.test/made-up"}]}],
        "pages": [{"url": "https://intra.test/leave", "kind": "list", "info": "휴가 사용 내역"}],
        "stores": {"pages": {"mode": "existing", "name": "docs", "reason": "사내 문서"}},
    }, ensure_ascii=False))
    made = _create(client)
    client.post(f"{API}/sources/{made['id']}/scan", headers=ADMIN)
    detail = client.get(f"{API}/sources/{made['id']}", headers=ADMIN).json()
    child = detail["scan"]["menu"][0]["children"][0]
    assert child["url"] == "" and child["label"] == "지어낸 메뉴"
    assert any("made-up" in n for n in detail["scan"]["notes"])
    pages = detail["proposal"]["targets"]["pages"]
    assert (pages["mode"], pages["store"], pages["reason"]) == ("existing", "docs", "사내 문서")
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


def _browser_file(source_id=1, **over):
    payload = {
        "agent": "gpax-scan/1", "source": {"id": source_id, "name": "인사 포털"},
        "origin": "https://intra.test", "url": "https://intra.test/main",
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
    sid = made["id"]
    url = f"{API}/sources/{sid}/browser-result"

    assert client.post(url, headers=ADMIN, files=_browser_file(sid, agent="x")).status_code == 400
    res = client.post(url, headers=ADMIN, files=_browser_file(sid, origin="https://other.test"))
    assert res.status_code == 400 and "다른 사이트" in res.json()["detail"]
    bad = {"file": ("a.json", b"not json", "application/json")}
    assert client.post(url, headers=ADMIN, files=bad).status_code == 400
    # 같은 사이트의 다른 출처 — 북마크가 여럿이면 잘못 올리기 쉽다.
    other = _create(client, name="인사 포털 2", headers="")
    res = client.post(f"{API}/sources/{other['id']}/browser-result", headers=ADMIN,
                      files=_browser_file(sid))
    assert res.status_code == 400 and "다른 출처의 북마크" in res.json()["detail"]

    res = client.post(url, headers=ADMIN, files=_browser_file(sid))
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
    result = infosource.from_browser(InfoSource(id=1, kind="web", url="https://intra.test/"), payload)
    assert [link["url"] for link in result["pages"][0]["links"]] == ["https://intra.test/leave"]


def test_browser_scan_only_for_web(client):
    made = _create(client, name="api", kind="api", url="https://intra.test/api", headers="")
    res = client.post(f"{API}/sources/{made['id']}/browser-result", headers=ADMIN,
                      files=_browser_file(made["id"]))
    assert res.status_code == 400


def test_api_source_reads_openapi(client, monkeypatch):
    spec = {"openapi": "3.0.0", "info": {"title": "재고 API"}, "paths": {
        "/items": {"get": {"summary": "품목 목록", "parameters": [{"name": "q"}],
                           "responses": {"200": {"content": {"application/json": {"schema": {
                               "type": "array", "items": {"$ref": "#/components/schemas/Item"}}}}}}},
                   "post": {"summary": "품목 등록"}}},
        "components": {"schemas": {"Item": {"properties": {"id": {}, "name": {}}}}}}

    def fake_get(url, headers, origin, **kw):
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


# ---------------------------------------------------------------- 내려받기 파일 · 유형별 저장 · 스캔 이력

PDF = ("application/pdf", b"%PDF-1.4 leave rules v1")
LEAVE_WITH_FILES = """<html><head><title>휴가 조회</title></head><body>
    <div class="lnb"><a href="/leave/history">사용 이력</a></div>
    <a href="/files/rules.pdf">휴가 규정</a> <a href="/files/days.xlsx">연차 표</a>
    <a href="/download.do?id=3">규정 내려받기</a> <a href="/files/missing.pdf">없는 파일</a>
    </body></html>"""


def _files_site(monkeypatch, pdf=PDF):
    monkeypatch.setitem(SITE, "https://intra.test/leave", LEAVE_WITH_FILES)
    monkeypatch.setitem(SITE, "https://intra.test/files/rules.pdf", pdf)
    # 확장자 없는 내려받기 — 내용이 rules.pdf와 같으면 한 벌로 친다.
    monkeypatch.setitem(SITE, "https://intra.test/download.do?id=3", pdf)
    monkeypatch.setitem(SITE, "https://intra.test/files/days.xlsx", (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", b"PK\x03\x04 sheet"))


def _scan(c, source_id):
    c.post(f"{API}/sources/{source_id}/scan", headers=ADMIN)
    detail = c.get(f"{API}/sources/{source_id}", headers=ADMIN).json()
    assert detail["status"] == "scanned", detail["error"]
    return detail


def test_downloads_are_saved_per_type_overwritten_and_trashed(client, monkeypatch):
    _files_site(monkeypatch)
    made = _create(client)
    detail = _scan(client, made["id"])
    files = {f["url"]: f for f in detail["scan"]["files"]}
    assert set(files) == {"https://intra.test/files/rules.pdf", "https://intra.test/files/days.xlsx"}
    assert files["https://intra.test/files/rules.pdf"]["category"] == "documents"
    assert files["https://intra.test/files/days.xlsx"]["category"] == "sheets"
    assert any("하나로 합쳤습니다" in n for n in detail["scan"]["notes"])
    assert any(s["url"].endswith("missing.pdf") for s in detail["scan"]["skipped"])
    targets = detail["proposal"]["targets"]
    assert list(targets) == ["pages", "documents", "sheets"]
    # 파일 유형에 따로 맞는 저장소가 없으면 정리와 같은 자리다.
    assert targets["documents"]["store"] == targets["pages"]["store"]

    keep = {"pages": {"mode": "existing", "store": "docs"}, "sheets": {"mode": "skip"}}
    res = _save(client, made["id"], **keep,
                documents={"mode": "new", "store": "rules", "path": str(client.tmp / "rules")})
    assert res.status_code == 200, res.text
    pdf = client.tmp / "rules" / "인사-포털" / "문서" / "rules.pdf"
    assert pdf.read_bytes() == PDF[1]
    assert not list(client.tmp.rglob("days.xlsx"))   # 저장 안 함
    index = (client.tmp / "docs" / "인사-포털" / "index.md").read_text(encoding="utf-8")
    assert "## 내려받은 파일" in index and "저장소 rules: 인사-포털/문서/rules.pdf" in index
    pages_dir = client.tmp / "docs" / "인사-포털" / "pages"
    pages = sorted(p.name for p in pages_dir.iterdir())

    # 그대로 다시 저장 — 바뀐 것이 없으니 아무것도 쓰지 않는다.
    rules = {"documents": {"mode": "existing", "store": "rules"}}
    again = _save(client, made["id"], **keep, **rules).json()
    assert again["files"] == [] and again["removed"] == []
    assert "인사-포털/문서/rules.pdf" in again["same"]

    # 파일이 바뀌면 같은 자리에 덮어쓴다. 다시 스캔하면 지난번 자리를 제안한다.
    _files_site(monkeypatch, pdf=("application/pdf", b"%PDF-1.4 leave rules v2"))
    targets = _scan(client, made["id"])["proposal"]["targets"]
    assert (targets["documents"]["mode"], targets["documents"]["store"]) == ("existing", "rules")
    out = _save(client, made["id"], **keep, **rules).json()
    assert out["files"] == ["인사-포털/문서/rules.pdf"]
    assert pdf.read_bytes().endswith(b"v2")
    assert sorted(p.name for p in pages_dir.iterdir()) == pages   # 화면 파일이 두 벌로 늘지 않는다

    # 사이트에서 없어진 파일은 휴지통으로.
    monkeypatch.setitem(SITE, "https://intra.test/leave", SITE["https://intra.test/leave/history"])
    _scan(client, made["id"])
    out = _save(client, made["id"], pages={"mode": "existing", "store": "docs"}).json()
    assert "rules:인사-포털/문서/rules.pdf" in out["removed"]
    assert not pdf.exists()
    assert (client.tmp / "rules" / storage.TRASH_DIRNAME / "인사-포털" / "문서" / "rules.pdf").exists()


def test_save_refuses_unknown_type_and_all_skipped(client):
    made = _create(client)
    _scan(client, made["id"])
    assert _save(client, made["id"], pages={"mode": "skip"}).status_code == 400
    res = _save(client, made["id"], slides={"mode": "existing", "store": "docs"})
    assert res.status_code == 400 and "없는 저장 유형" in res.json()["detail"]


def test_legacy_numbered_pages_are_replaced_not_duplicated(client):
    made = _create(client)
    _scan(client, made["id"])
    legacy = client.tmp / "docs" / "인사-포털" / "pages"
    legacy.mkdir(parents=True)
    (legacy / "01-인사-포털.md").write_text("# 옛 이름", encoding="utf-8")
    from app.db import SessionLocal
    from app.models import InfoSource

    with SessionLocal() as s:   # 저장 목록을 남기기 전에 저장한 출처
        row = s.get(InfoSource, made["id"])
        row.target_store, row.saved_files = "docs", None
        s.commit()
    out = _save(client, made["id"], pages={"mode": "existing", "store": "docs"}).json()
    assert "docs:인사-포털/pages/01-인사-포털.md" in out["removed"]
    assert not (legacy / "01-인사-포털.md").exists()


def test_scan_history_marks_changes_and_rests_missing_pages(client, fake_site, monkeypatch):
    monkeypatch.setitem(SITE, "https://intra.test/", SITE["https://intra.test/"].replace(
        '<a href="/pay">', '<a href="/old">옛 메뉴</a><a href="/pay">'))
    made = _create(client)
    _scan(client, made["id"])
    assert "https://intra.test/old" in [u for u, _ in fake_site]   # 처음엔 받아 본다(404)

    fake_site.clear()
    monkeypatch.setitem(SITE, "https://intra.test/leave/history",
                        "<html><head><title>사용 이력</title></head><body>바뀐 이력</body></html>")
    detail = _scan(client, made["id"])
    # 지난번에 없던 주소는 한 번 쉰다.
    assert "https://intra.test/old" not in [u for u, _ in fake_site]
    rested = next(s for s in detail["scan"]["skipped"] if s["url"] == "https://intra.test/old")
    assert "건너뜀" in rested["reason"]
    change = {p["url"]: p["change"] for p in detail["scan"]["pages"]}
    assert change["https://intra.test/leave/history"] == "changed"
    assert change["https://intra.test/leave"] == "same"
    assert any("지난 스캔과 비교" in n for n in detail["scan"]["notes"])

    fake_site.clear()
    _scan(client, made["id"])
    assert "https://intra.test/old" in [u for u, _ in fake_site]   # 쉰 다음에는 다시 본다

    scans = client.get(f"{API}/sources/{made['id']}/scans", headers=ADMIN).json()
    assert [s["via"] for s in scans] == ["server"] * 3
    assert scans[1]["changes"]["changed"] == 1
    assert scans[2]["changes"]["new"] == scans[2]["pages"]

    client.delete(f"{API}/sources/{made['id']}", headers=ADMIN)
    from app.db import SessionLocal
    from app.models import InfoSourceScan

    with SessionLocal() as s:
        assert s.query(InfoSourceScan).filter(InfoSourceScan.source_id == made["id"]).count() == 0


def test_browser_scan_carries_files(client, monkeypatch):
    import base64

    monkeypatch.setattr(infosource, "_submit_result", infosource._result_job)
    made = _create(client, headers="")
    data = base64.b64encode(b"%PDF-1.4 guide").decode()
    files = [
        {"url": "https://intra.test/download.do?id=7", "name": "안내서.pdf", "text": "안내",
         "type": "application/pdf", "data": data},
        {"url": "https://other.test/a.pdf", "name": "a.pdf", "data": data},
        {"url": "https://intra.test/setup.exe", "name": "setup.exe", "data": data},
        {"url": "https://intra.test/broken.pdf", "name": "broken.pdf", "data": "@@not base64@@"},
    ]
    res = client.post(f"{API}/sources/{made['id']}/browser-result", headers=ADMIN,
                      files=_browser_file(made["id"], files=files))
    assert res.status_code == 202, res.text
    scan = client.get(f"{API}/sources/{made['id']}", headers=ADMIN).json()["scan"]
    assert [(f["name"], f["category"]) for f in scan["files"]] == [("안내서.pdf", "documents")]
    reasons = {s["url"]: s["reason"] for s in scan["skipped"]}
    assert "받지 않는 형식" in reasons["https://intra.test/setup.exe"]
    assert "깨져" in reasons["https://intra.test/broken.pdf"]
    assert "https://other.test/a.pdf" not in reasons   # 다른 출처는 아예 받지 않는다
    scans = client.get(f"{API}/sources/{made['id']}/scans", headers=ADMIN).json()
    assert scans[0]["via"] == "browser" and scans[0]["files"] == 1
