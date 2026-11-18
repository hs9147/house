"""정보 현황(services/coverage) — 관리하는 데이터의 종류·유형, 사이트에서 받을 수 있는 정보, 신규 판정, 사람 입력 없는 제안 → 확정.

판정은 규칙이 먼저다(같은 주소 · 결과 열 Jaccard — Table Union Search · 이름 유사도), LLM은 애매한 것(비슷함·신규)만
다시 본다. 제안은 신규 화면마다 받을 것·조건·스킬 초안까지 만들고, 사람은 확정만 한다.
"""
import json
from datetime import date

from app.db import SessionLocal
from app.models import InfoSource
from app.services import coverage, infosource
from tests.test_sources_api import ADMIN, API, _browser_file, _create, client, fake_site  # noqa: F401 — 픽스처

PAGES = [
    {"url": "https://intra.test/notice", "title": "공지 조회", "fields": ["등록일"], "tables": [["번호", "제목"]], "links": []},
    {"url": "https://intra.test/contract", "title": "구매 계약 현황", "fields": ["계약일"],
     "tables": [["계약번호", "업체", "금액"]], "links": []},
    {"url": "https://intra.test/guide", "title": "이용 안내", "links": [], "text": "안내 본문"},
    {"url": "https://intra.test/notices", "title": "공지 조회 목록", "fields": ["구분"], "tables": [["구분", "내용"]],
     "links": []},
    {"url": "https://intra.test/login", "title": "로그인", "requires_login": True, "links": []},
]


def _scanned(client, monkeypatch, name, pages=PAGES):
    monkeypatch.setattr(infosource, "_submit_result", infosource._result_job)
    made = _create(client, headers="", name=name)
    res = client.post(f"{API}/sources/{made['id']}/browser-result", headers=ADMIN, files=_browser_file(made["id"], pages=pages))
    assert res.status_code == 202, res.text
    return made["id"]


def _setup(client, monkeypatch):
    """출처 A — 공지 조회를 이미 받는다. 출처 B — 계약 목록(계약번호·업체·금액)을 받아 누적했다."""
    a = _scanned(client, monkeypatch, "인사 포털")
    client.put(f"{API}/sources/{a}/queries", headers=ADMIN, json={"screens": [
        {"url": "https://intra.test/notice", "conditions": [{"field": "등록일", "value": "@today"}],
         "skill": {"name": "공지"}}]})
    b = _scanned(client, monkeypatch, "구매 포털", [{"url": "https://intra.test/c2", "title": "계약 목록",
                                               "fields": ["기간"], "tables": [["계약번호", "업체", "금액"]], "links": []}])
    client.put(f"{API}/sources/{b}/queries", headers=ADMIN, json={"screens": [
        {"url": "https://intra.test/c2", "conditions": []}]})
    cols = [{"key": k, "label": k} for k in ("계약번호", "업체", "금액")]
    infosource.merge_dataset(b, {"screen": "계약 목록", "url": "https://intra.test/c2", "key": "계약번호", "columns": cols,
                                 "records": [{"계약번호": "C1", "업체": "에이", "금액": 1}], "total": 1, "complete": True,
                                 "since": ""}, date.today().isoformat())
    return a, b


def test_inventory_lists_what_each_source_manages_by_type(client, monkeypatch):
    a, b = _setup(client, monkeypatch)
    inv = client.get(f"{API}/sources/inventory", headers=ADMIN).json()
    by = {s["id"]: s for s in inv["sources"]}
    assert [sc["type"] for sc in by[a]["screens"]] == ["조회 화면"] and by[a]["screens"][0]["skill"] == "공지"
    assert by[b]["screens"][0]["type"] == "목록" and by[b]["screens"][0]["dataset"]["columns"] == ["계약번호", "업체", "금액"]
    assert inv["totals"]["목록"] >= 1 and inv["totals"]["스킬"] >= 1 and inv["kinds"]["web"] >= 2


def test_discovered_items_are_judged_managed_duplicate_similar_or_new(client, monkeypatch):
    a, _b = _setup(client, monkeypatch)
    with SessionLocal() as db:
        out = coverage.discover(db, db.get(InfoSource, a))
    st = {i["url"]: (i["status"], i["reason"]) for i in out["items"]}
    assert st["https://intra.test/notice"][0] == "managed"
    assert st["https://intra.test/contract"][0] == "duplicate" and "겹칩니다" in st["https://intra.test/contract"][1]
    assert st["https://intra.test/notices"][0] == "similar"          # 「공지 조회」와 이름이 비슷하다
    assert st["https://intra.test/guide"][0] == "new"
    assert "https://intra.test/login" not in st                      # 로그인 화면은 받을 정보가 아니다


def test_proposal_needs_no_input_and_the_admin_only_confirms(client, monkeypatch):
    """버튼 하나 — 판정(애매한 것은 LLM), 신규 화면의 받을 것·조건·스킬 초안까지 제안. 확정하면 그대로 수집 조건에."""
    a, _b = _setup(client, monkeypatch)
    monkeypatch.setattr(coverage, "_submit", coverage._proposal_job)
    asked = []

    def judge(provider, messages, db=None, **kw):
        text = messages[-1]["content"]
        asked.append(text[:40])
        if "이미 관리하는 정보와 같은 것인지" in text:     # 공지 조회 목록은 공지 조회와 다른 정보라고 본다
            found = json.loads(text.split("새로 찾은 화면:")[1])
            return json.dumps({"items": [{"i": f["i"], "status": "new", "reason": "구분별 목록이다"}
                                         for f in found if f["title"] == "공지 조회 목록"]}, ensure_ascii=False)
        return "{}"                                           # 수집 방법·스킬 초안 — 규칙으로 떨어진다

    monkeypatch.setattr(coverage.llm, "default_provider", lambda db, organization_id=None: object())
    monkeypatch.setattr(coverage.llm, "chat_completion", judge)
    assert client.post(f"{API}/sources/{a}/coverage", headers=ADMIN).status_code == 202
    got = client.get(f"{API}/sources/{a}/coverage", headers=ADMIN).json()["proposal"]
    assert got["judged_by"] == "llm" and not got.get("error")
    proposed = {p["url"]: p for p in got["proposed"]}
    assert set(proposed) == {"https://intra.test/guide", "https://intra.test/notices"}   # 중복·관리 중은 빼고
    assert proposed["https://intra.test/guide"]["screen"]["collect"] == "page"           # 조회 칸이 없는 화면은 본문
    assert proposed["https://intra.test/notices"]["screen"]["skill"]["name"]             # 조회 화면은 스킬 초안까지
    res = client.post(f"{API}/sources/{a}/coverage/confirm", headers=ADMIN,
                      json={"urls": ["https://intra.test/notices"]})                     # 하나만 확정
    assert res.status_code == 200, res.text
    screens = {s["url"]: s for s in client.get(f"{API}/sources/{a}/queries", headers=ADMIN).json()["screens"]}
    assert "https://intra.test/notices" in screens and "https://intra.test/guide" not in screens
    assert screens["https://intra.test/notices"]["skill"]["name"]
    assert client.get(f"{API}/sources/{a}/coverage", headers=ADMIN).json()["proposal"] is None
    assert client.post(f"{API}/sources/{a}/coverage/confirm", headers=ADMIN, json={"urls": []}).status_code == 400


def test_harness_rounds_cross_check_the_source_and_the_stores_and_take_corrections(client, monkeypatch):
    """하네스 — 출처의 발견과 저장소의 유형을 서로 교정하며 되풀이한다.
    출처 → 저장소: 새 화면의 결과 열이 저장소의 되풀이되는 표와 겹치면 비슷함(이미 들어와 있을 수 있다).
    저장소 → 출처: 되풀이되는 표인데 어느 출처도 덮지 않으면 출처 미확인.
    수집 → 판정: 막힌 화면은 교정할 것. 사람의 교정은 다음 라운드부터 먼저 쓴다. 스캔이 끝나면 라운드가 저절로 돈다."""
    from app.api import mcp_servers
    from app.services import docsearch

    root = client.tmp / "docs"
    for i in range(5):     # 저장소에 되풀이되는 표 두 가지 — 하나는 출처 화면과 겹치고, 하나는 출처가 없다
        (root / f"공지대장{i}.md").write_text(f"# 대장 {i}\n\n| 구분 | 내용 |\n|---|---|\n| 인사 | 휴가 |\n", encoding="utf-8")
        (root / f"재고표{i}.md").write_text(f"# 재고 {i}\n\n| 품번 | 수량 | 창고 |\n|---|---|---|\n| A | 1 | 1 |\n",
                                         encoding="utf-8")
    docsearch.reindex("docs", root)
    a, _b = _setup(client, monkeypatch)       # 브라우저 스캔이 끝나며 라운드가 이미 한 번 돌았다
    h = client.get(f"{API}/sources/{a}/coverage", headers=ADMIN).json()["harness"]
    assert h["round"]["trigger"].startswith("스캔")
    with SessionLocal() as db:
        row = db.get(InfoSource, a)
        row.scan = {**row.scan, "collected": [{"url": "https://intra.test/notice", "screen": "공지 조회", "rows": 0,
                                               "notes": ["화면으로 가는 1번째 단계를 찾지 못했습니다"],
                                               "repair": {"kind": "step"}}]}
        db.commit()
    h = client.post(f"{API}/sources/{a}/coverage/round", headers=ADMIN).json()
    items = {i["url"]: i for i in h["items"]}
    notices = items["https://intra.test/notices"]
    assert notices["status"] == "similar" and notices["store_match"]["store"] == "docs"
    assert notices["store_match"]["documents"] == 5
    assert any(g["name"] == "품번 | 수량 | 창고" for g in h["gaps"])                     # 출처 미확인
    assert not any(g["name"] == "구분 | 내용" for g in h["gaps"])                        # 출처 화면이 덮는다
    assert [i["kind"] for i in h["issues"]] == ["막힘"]
    # 사람의 교정 — 다음 라운드부터 먼저, 바뀐 판정이 기록된다
    h = client.post(f"{API}/sources/{a}/coverage/decide", headers=ADMIN,
                    json={"url": "https://intra.test/notices", "status": "new"}).json()
    assert {i["url"]: i for i in h["items"]}["https://intra.test/notices"]["status"] == "new"
    assert h["round"]["changed"] >= 1 and h["round"]["decided"] == 1 and len(h["rounds"]) >= 3
    with SessionLocal() as db:
        out = json.loads(mcp_servers._ops_call(db, "admin", "source_coverage", {}))
    assert any(o["source"] == "인사 포털" and o["round"]["gaps"] >= 1 for o in out)
    assert client.post(f"{API}/sources/{a}/coverage/decide", headers=ADMIN,
                       json={"url": "x", "status": "maybe"}).status_code == 400
