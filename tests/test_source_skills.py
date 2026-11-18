"""수집 스킬 — 데이터 수집의 화면 하나를 에이전트가 골라 부르는 일로(services/skills).

스킬은 수집 조건의 화면에 붙는다(이름·용도·입력·출력·공개 범위). 실행은 서버 봇(services/sourcebot — 북마크와
같은 엔진)이 하는데, 여기서는 봇의 경계(sourcebot.run)를 가짜로 바꿔 입력이 조건에 들어가는지·권한·결과 모양·
무인 수집을 본다. 엔진을 실제 브라우저로 돌리는 시험은 맨 아래(브라우저가 없으면 건너뛴다).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.models import InfoSource, Organization, UserAccount
from app.services import infosource, skills, sourcebot
from tests.test_sources_api import (  # noqa: F401 — 픽스처
    ADMIN, API, _browser_file, _create, _notice_scan, _save, client, fake_site,
)

NOTICE = "https://intra.test/notice"
SKILL = {"name": "공지 검색", "description": "공지 목록", "when": "공지를 찾을 때",
         "params": [{"name": "구분", "description": "공지 구분", "default": "전체"}]}


def _with_skill(client, sid, skill=SKILL, conditions=None):
    screen = {"url": NOTICE, "conditions": conditions if conditions is not None else [
        {"field": "등록일", "value": "@last|@today-3y"}, {"field": "구분", "value": "{구분}"}], "skill": skill}
    return client.put(f"{API}/sources/{sid}/queries", headers=ADMIN, json={"screens": [screen]})


def _payload(sid, records, **over):
    cols = [{"key": "no", "label": "번호"}, {"key": "title", "label": "제목"}]
    return {"agent": "gpax-scan/1", "source": {"id": sid, "name": "인사 포털"}, "origin": "https://intra.test",
            "url": "https://intra.test/", "replay": True, "menu": [], "pages": [], "files": [],
            "collected": [{"screen": "공지 조회", "url": NOTICE, "rows": len(records), "details": 0, "files": 0}],
            "datasets": [{"screen": "공지 조회", "url": NOTICE, "key": "no", "columns": cols, "records": records,
                          "total": len(records), "complete": True, "since": ""}],
            "captures": [], "queries_version": "", **over}


@pytest.fixture
def fake_bot(monkeypatch):
    calls = []

    def run(source, screens, headers, *, start_url, timeout, step=lambda _t: None):
        calls.append({"screens": screens, "start": start_url, "timeout": timeout})
        return _payload(source["id"], [{"no": 1, "title": "휴가 안내"}, {"no": 2, "title": "보안 점검"}])

    monkeypatch.setattr(sourcebot, "run", run)
    monkeypatch.setattr(skills, "runnable", lambda: (True, ""))
    skills._CACHE.clear()
    return calls


def _member(client, org_id=None):
    """비관리자 키 — 이름이 계정 이메일이면 그 계정의 조직이 권한이다(security.viewer_org_ids)."""
    import uuid

    email = f"member-{uuid.uuid4().hex[:8]}@lge.com"
    key = client.post(f"{API}/keys", json={"name": email, "is_admin": False}, headers=ADMIN).json()["key"]
    with SessionLocal() as db:
        user = UserAccount(email=email, name="멤버", password_hash="x", is_approved=True)
        if org_id:
            user.organizations.append(db.get(Organization, org_id))
        db.add(user)
        db.commit()
    return {"x-api-key": key}


def test_skill_rides_on_its_screen_and_placeholders_must_be_inputs(client, monkeypatch):
    sid = _notice_scan(client, monkeypatch)
    res = _with_skill(client, sid)
    assert res.status_code == 200, res.text
    skill = res.json()["screens"][0]["skill"]
    assert skill["name"] == "공지-검색" and skill["params"][0] == {
        "name": "구분", "description": "공지 구분", "default": "전체", "required": False}
    assert skill["audience"] == []                     # 기본은 관리자만
    # 북마크·봇이 도는 조건은 자리를 기본값으로 채운 것 — 글자 {구분}을 칸에 넣지 않는다
    served = client.get(f"{API}/sources/{sid}", headers=ADMIN).json()["queries"][0]
    assert "skill" not in served and {"field": "구분", "value": "전체"} in served["conditions"]
    # 입력으로 정의하지 않은 자리, 두 화면에 같은 이름
    bad = _with_skill(client, sid, conditions=[{"field": "구분", "value": "{없는것}"}])
    assert bad.status_code == 400 and "없는것" in bad.json()["detail"]
    two = {"screens": [{"url": NOTICE, "conditions": [], "skill": {"name": "같은"}},
                       {"url": "https://intra.test/main", "conditions": [], "skill": {"name": "같은"}}]}
    res = client.put(f"{API}/sources/{sid}/queries", headers=ADMIN, json=two)
    assert res.status_code == 400 and "같은 이름의 스킬" in res.json()["detail"]


def _draft(sid: int, url: str) -> dict:
    """스킬 초안(skills.draft) — 정보 현황의 제안이 신규 조회 화면에 쓴다."""
    with SessionLocal() as db:
        return skills.draft(db, db.get(InfoSource, sid), url)


def test_draft_turns_fixed_values_into_inputs_and_tables_into_outputs(client, monkeypatch):
    """LLM이 없을 때의 초안 — 상대 날짜가 아닌 칸은 부를 때 바꿀 입력으로, 결과 표의 머리글은 출력으로."""
    sid = _notice_scan(client, monkeypatch)
    client.put(f"{API}/sources/{sid}/queries", headers=ADMIN, json={"screens": [{"url": NOTICE, "conditions": [
        {"field": "등록일", "value": "@today-1m"}, {"field": "구분", "value": "인사"}]}]})
    out = _draft(sid, NOTICE)
    assert out["via"] == "rule" and out["skill"]["name"] == "공지-조회"
    assert [p["name"] for p in out["skill"]["params"]] == ["구분"] and out["skill"]["params"][0]["default"] == "인사"
    assert out["conditions"] == [{"field": "등록일", "value": "@today-1m"}, {"field": "구분", "value": "{구분}"}]
    assert [o["name"] for o in out["skill"]["outputs"]] == ["번호", "제목"]
    # 초안 그대로 저장된다
    saved = client.put(f"{API}/sources/{sid}/queries", headers=ADMIN, json={"screens": [
        {"url": NOTICE, "conditions": out["conditions"], "skill": out["skill"]}]})
    assert saved.status_code == 200, saved.text
    with pytest.raises(infosource.SourceError):
        _draft(sid, "https://intra.test/none")


def test_draft_from_llm_keeps_to_the_facts(client, monkeypatch):
    sid = _notice_scan(client, monkeypatch)
    client.put(f"{API}/sources/{sid}/queries", headers=ADMIN, json={"screens": [{"url": NOTICE, "conditions": [
        {"field": "등록일", "value": "@today-1m"}]}]})
    answer = {"name": "notice-search", "description": "공지 목록을 찾는다", "when": "공지를 물을 때",
              "params": [{"field": "구분", "name": "kind", "description": "구분"},
                         {"field": "없는 칸", "name": "ghost"}],
              "outputs": [{"name": "제목", "description": "공지 제목"}, {"name": "지어낸 열"}]}
    monkeypatch.setattr(skills.llm, "default_provider", lambda db, organization_id=None: object())
    monkeypatch.setattr(skills.llm, "chat_completion", lambda *a, **kw: json.dumps(answer, ensure_ascii=False))
    out = _draft(sid, NOTICE)
    assert out["via"] == "llm" and out["skill"]["name"] == "notice-search"
    assert [p["name"] for p in out["skill"]["params"]] == ["kind"]                 # 없는 칸은 버린다
    assert out["conditions"][-1] == {"field": "구분", "value": "{kind}"}           # 조건에 자리가 생긴다
    assert [o["name"] for o in out["skill"]["outputs"]] == ["제목"]                # 지어낸 열은 버린다


def test_run_fills_the_inputs_returns_rows_and_caches(client, monkeypatch, fake_bot):
    sid = _notice_scan(client, monkeypatch)
    _with_skill(client, sid)
    listed = client.get(f"{API}/skills", headers=ADMIN).json()
    [skill] = listed["skills"]
    assert skill["id"] == "인사 포털/공지-검색" and listed["runnable"] is True
    res = client.post(f"{API}/skills/run", headers=ADMIN, json={"skill": skill["id"], "params": {"구분": "보안"}})
    assert res.status_code == 200, res.text
    out = res.json()
    sent = fake_bot[0]["screens"][0]
    assert {"field": "구분", "value": "보안"} in sent["conditions"] and "skill" not in sent
    assert out["lists"][0]["columns"] == ["번호", "제목"] and out["lists"][0]["rows"][0] == {"번호": 1, "제목": "휴가 안내"}
    assert out["lists"][0]["got"] == 2 and out["cached"] is False
    again = client.post(f"{API}/skills/run", headers=ADMIN, json={"skill": skill["id"], "params": {"구분": "보안"}})
    assert again.json()["cached"] is True and len(fake_bot) == 1
    # 기본값으로 — 다른 입력이라 다시 돈다
    client.post(f"{API}/skills/run", headers=ADMIN, json={"skill": skill["id"]})
    assert {"field": "구분", "value": "전체"} in fake_bot[1]["screens"][0]["conditions"]
    bad = client.post(f"{API}/skills/run", headers=ADMIN, json={"skill": skill["id"], "params": {"모름": "x"}})
    assert bad.status_code == 400 and "없는 입력" in bad.json()["detail"]
    assert client.post(f"{API}/skills/run", headers=ADMIN, json={"skill": "인사 포털/없음"}).status_code == 400

    from app.models import AuditEvent
    with SessionLocal() as db:
        events = db.query(AuditEvent).filter(AuditEvent.action == "source.skill.run").all()
        assert events and all("보안" not in json.dumps(e.detail, ensure_ascii=False) for e in events)


def test_audience_decides_who_sees_and_runs_a_skill(client, monkeypatch, fake_bot):
    """스킬은 맡긴 세션으로 사이트에 들어간다 — 기본은 관리자만, 공개 범위의 조직 사람만 보고 부른다(MCP도)."""
    sid = _notice_scan(client, monkeypatch)
    _with_skill(client, sid)
    member = _member(client, client.org_id)
    outsider = _member(client)
    assert client.get(f"{API}/skills", headers=member).json()["skills"] == []
    res = client.post(f"{API}/skills/run", headers=member, json={"skill": "인사 포털/공지-검색"})
    assert res.status_code == 400 and "권한" in res.json()["detail"]

    _with_skill(client, sid, skill={**SKILL, "audience": [client.org_id]})
    assert [s["id"] for s in client.get(f"{API}/skills", headers=member).json()["skills"]] == ["인사 포털/공지-검색"]
    assert client.get(f"{API}/skills", headers=outsider).json()["skills"] == []

    def rpc(headers, method, params=None):
        return client.post(f"{API}/mcp/skills", headers=headers,
                           json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).json()

    tools = rpc(member, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["list", "run"]
    listed = json.loads(rpc(member, "tools/call", {"name": "list", "arguments": {}})["result"]["content"][0]["text"])
    assert [s["id"] for s in listed["skills"]] == ["인사 포털/공지-검색"]
    ran = rpc(member, "tools/call", {"name": "run", "arguments": {"skill": "인사 포털/공지-검색",
                                                                  "params": {"구분": "인사"}, "rows": 1}})
    body = json.loads(ran["result"]["content"][0]["text"])
    assert len(body["lists"][0]["rows"]) == 1 and body["lists"][0]["got"] == 2
    denied = rpc(outsider, "tools/call", {"name": "run", "arguments": {"skill": "인사 포털/공지-검색"}})
    assert "권한" in denied["error"]["message"]


def test_server_bot_runs_every_screen_and_lands_like_the_bookmark(client, monkeypatch, fake_bot):
    monkeypatch.setattr(infosource, "_submit_bot", infosource._bot_job)
    sid = _notice_scan(client, monkeypatch)
    _with_skill(client, sid)
    res = client.post(f"{API}/sources/{sid}/bot", headers=ADMIN)
    assert res.status_code == 202, res.text
    detail = client.get(f"{API}/sources/{sid}", headers=ADMIN).json()
    assert detail["status"] == "scanned", detail["error"]
    assert any(n.startswith("서버 봇") for n in detail["scan"]["notes"])
    assert any(f["name"] == "공지-조회 목록.csv" for f in detail["scan"]["files"])
    assert {"field": "구분", "value": "전체"} in fake_bot[0]["screens"][0]["conditions"]   # 기본값으로 돈다


def test_unattended_collect_uses_the_bot_for_screens_a_server_scan_cannot_take(client, monkeypatch, fake_bot):
    """메뉴 길·「파일」로 받는 화면은 서버 스캔이 못 받는다 — 무인 수집이 서버 봇으로 받아 지난 자리에 저장한다.
    북마크로 읽은 출처도 수집 조건이 있으면 들어온다(봇은 화면 목록을 덮지 않는다)."""
    monkeypatch.setenv("PAAS_SOURCE_UNATTENDED", "true")
    get_settings.cache_clear()
    sid = _notice_scan(client, monkeypatch)
    with SessionLocal() as db:
        assert infosource.collectable(db) == []            # 저장한 적이 없다
    assert _save(client, sid, pages={"mode": "existing", "store": "docs"}).status_code == 200
    with SessionLocal() as db:
        assert infosource.collectable(db) == []            # 북마크로 읽었고 수집 조건이 없다 — 서버 스캔이 덮는다
    _with_skill(client, sid)
    with SessionLocal() as db:
        row = db.get(InfoSource, sid)
        assert [r.id for r in infosource.collectable(db)] == [sid] and infosource.by_bot(row)
        out = infosource.collect(db, row)
    assert len(fake_bot) == 1 and out["pages"] >= 1
    assert out["unsaved"] == ["sheets"]       # 새로 생긴 유형(목록 CSV)은 위치를 사람이 정한다


# ---------------------------------------------------------------- 엔진을 실제 브라우저로

FORM = """<html><head><title>공지 조회</title></head><body>
<form onsubmit="return false"><label for="kind">구분</label><input id="kind" name="kind">
<button type="button" onclick="show()">조회</button></form>
<table id="out"><tr><th>번호</th><th>제목</th></tr></table>
<script>function show(){var v=document.getElementById('kind').value;
document.getElementById('out').innerHTML='<tr><th>번호</th><th>제목</th></tr><tr><td>1</td><td>'+v+' 안내</td></tr>';}
</script></body></html>"""


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = FORM if self.path.split("?")[0] in ("/", "/notice") else ""
        self.send_response(200 if body else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_engine_runs_in_the_server_browser_and_hands_back_the_result(site):
    """북마크 엔진이 서버 브라우저에서 받는 창(가짜 출처)과 인사하고, 조건을 채워 조회한 결과를 돌려준다."""
    if not infosource.browser_available() or not sourcebot.engine_available():
        pytest.skip("playwright·엔진 없음")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        try:
            browser, _ = infosource._launch(pw)
        except infosource.SourceError as e:
            pytest.skip(str(e))
        browser.close()
    steps = []
    screen = {"screen": "공지 조회", "url": f"{site}/notice", "conditions": [{"field": "구분", "value": "보안"}]}
    payload = sourcebot.run({"id": 7, "name": "시험"}, [screen], {}, start_url=f"{site}/", timeout=90,
                            step=steps.append)
    assert payload["agent"] == "gpax-scan/1" and payload["replay"] is True
    assert payload["source"]["id"] == 7 and payload["origin"] == site
    [got] = payload["collected"]
    assert got["url"] == f"{site}/notice" and got["rows"] >= 1, got
    assert steps        # 엔진의 기록이 실시간으로 왔다


def test_chat_proposes_changes_that_stay_within_the_facts(client, monkeypatch):
    """스킬 대화 — LLM이 답과 고칠 것을 낸다. 화면에 없는 칸·모르는 화면·정의 안 된 자리는 서버가 거르고, 저장하지 않는다.
    LLM이 받는 사실에는 편집 중인 것(저장 전)이 실린다."""
    sid = _notice_scan(client, monkeypatch)
    client.put(f"{API}/sources/{sid}/queries", headers=ADMIN, json={"screens": [{"url": NOTICE, "conditions": [
        {"field": "등록일", "value": "@today-1m"}]}]})
    seen = {}
    answer = {"reply": "구분을 입력으로 두었습니다.", "changes": [
        {"url": NOTICE, "conditions": [{"field": "등록일", "value": "@today-1m"}, {"field": "구분", "value": "{kind}"}],
         "skill": {"name": "notice", "description": "공지 목록", "when": "공지를 물을 때",
                   "params": [{"name": "kind", "description": "구분", "default": "전체"}]}},
        {"url": NOTICE + "#x", "skill": {"name": "ghost"}},
    ]}

    def chat(provider, messages, db=None, **kw):
        seen["messages"] = messages
        return json.dumps(answer, ensure_ascii=False)

    monkeypatch.setattr(skills.llm, "default_provider", lambda db, organization_id=None: object())
    monkeypatch.setattr(skills.llm, "chat_completion", chat)
    draft = [{"url": NOTICE, "screen": "공지(편집 중)", "conditions": [{"field": "등록일", "value": "@today-1m"}]}]
    res = client.post(f"{API}/sources/{sid}/skills/chat", headers=ADMIN, json={
        "messages": [{"role": "user", "content": "구분을 입력으로 해 줘"}], "screens": draft})
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["reply"] == "구분을 입력으로 두었습니다."
    assert "공지(편집 중)" in seen["messages"][0]["content"] and seen["messages"][-1]["role"] == "user"
    first = out["changes"][0]
    assert first["skill"]["name"] == "notice" and first["conditions"][-1] == {"field": "구분", "value": "{kind}"}
    assert len(out["changes"]) == 1 and any("모르는 화면" in n for n in out["notes"])
    # 저장하지 않는다
    assert "skill" not in client.get(f"{API}/sources/{sid}/queries", headers=ADMIN).json()["screens"][0]
    # 정의 안 된 자리는 입력으로 더하고, 화면에 없는 칸이면 조건을 그대로 둔다
    answer["changes"] = [{"url": NOTICE, "conditions": [{"field": "구분", "value": "{nope}"}], "skill": {"name": "x"}}]
    out = client.post(f"{API}/sources/{sid}/skills/chat", headers=ADMIN, json={
        "messages": [{"role": "user", "content": "다시"}]}).json()
    assert [p["name"] for p in out["changes"][0]["skill"]["params"]] == ["nope"]
    answer["changes"] = [{"url": NOTICE, "conditions": [{"field": "없는 칸", "value": "1"}]}]
    out = client.post(f"{API}/sources/{sid}/skills/chat", headers=ADMIN, json={
        "messages": [{"role": "user", "content": "또"}]}).json()
    assert out["changes"][0]["conditions"] == [{"field": "등록일", "value": "@today-1m"}]
    assert any("없는 칸" in n for n in out["notes"])
    assert client.post(f"{API}/sources/{sid}/skills/chat", headers=ADMIN,
                       json={"messages": []}).status_code == 400


def test_chat_declares_placeholders_the_llm_forgot_as_inputs(client, monkeypatch):
    """LLM이 조건에 {자리}를 쓰고 입력으로 정의하지 않은 경우(실측 buyer-contract-status: CODE_ITEM … KEYWORD_ITEM) —
    자리를 입력으로 더해 받는다(거르지 않는다). 사람이 저장할 때는 고치는 방법을 알린다."""
    sid = _notice_scan(client, monkeypatch)
    client.put(f"{API}/sources/{sid}/queries", headers=ADMIN, json={"screens": [{"url": NOTICE, "conditions": []}]})
    names = ["CODE_ITEM", "CODE_ITEM2", "CODE_ITEM3", "CODE_ITEM4", "CODE_ITEM5", "CODE_ITEM6", "DATE_ITEM", "KEYWORD_ITEM"]
    answer = {"reply": "입력으로 두었습니다.", "changes": [{
        "url": NOTICE, "conditions": [{"field": "구분", "value": "{" + n + "}"} for n in names],
        "skill": {"name": "buyer-contract-status", "description": "계약 현황",
                  "params": [{"name": "keyword", "description": "검색어"}]}}]}
    monkeypatch.setattr(skills.llm, "default_provider", lambda db, organization_id=None: object())
    monkeypatch.setattr(skills.llm, "chat_completion", lambda *a, **kw: json.dumps(answer))
    out = client.post(f"{API}/sources/{sid}/skills/chat", headers=ADMIN,
                      json={"messages": [{"role": "user", "content": "입력을 정해 줘"}]}).json()
    [change] = out["changes"]
    assert [p["name"] for p in change["skill"]["params"]] == ["keyword", *names]
    assert any("입력으로 더했습니다" in n for n in out["notes"])
    saved = client.put(f"{API}/sources/{sid}/queries", headers=ADMIN, json={"screens": [
        {"url": NOTICE, "conditions": change["conditions"], "skill": change["skill"]}]})
    assert saved.status_code == 200, saved.text
    bad = _with_skill(client, sid, conditions=[{"field": "구분", "value": "{CODE_ITEM}"}])
    assert bad.status_code == 400 and "같은 이름의 입력을 더하거나" in bad.json()["detail"]


def test_list_columns_are_measured_fixed_or_changing(client, monkeypatch, fake_bot):
    """고정부·변경부를 재서 가른다 — 같은 건을 다시 받을 때 열마다 바뀐 비율. 거의 안 바뀌는 열은 고정(지식 후보),
    자주 바뀌는 열은 변경(상태 후보). 다시 받아 본 수가 적으면 모름."""
    from datetime import date

    sid = _notice_scan(client, monkeypatch)
    _with_skill(client, sid)
    cols = [{"key": "no", "label": "번호"}, {"key": "vendor", "label": "협력사"}, {"key": "state", "label": "상태"}]
    for run in range(25):
        recs = [{"no": i, "vendor": f"협력사{i}", "state": f"단계{(run + i) % 3}"} for i in range(4)]
        infosource.merge_dataset(sid, {"screen": "공지 조회", "url": NOTICE, "key": "no", "columns": cols,
                                       "records": recs, "total": 4, "complete": True, "since": ""},
                                 date.today().isoformat())
    vol = {v["name"]: v for v in infosource.column_volatility(sid, NOTICE)}
    assert vol["협력사"]["hint"] == "fixed" and vol["번호"]["hint"] == "fixed"
    assert vol["상태"]["hint"] == "changing" and vol["상태"]["rate"] > 0.5
    [skill] = client.get(f"{API}/skills", headers=ADMIN).json()["skills"]
    marks = {o["name"]: o.get("volatility") for o in skill["outputs"]}
    assert marks == {"번호": "fixed", "협력사": "fixed", "상태": "changing"}
