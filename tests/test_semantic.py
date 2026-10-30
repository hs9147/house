"""내용 온톨로지(services/semantic) — 제안은 LLM이 검토하고, 추출은 기계로 대조하고, 표본은 LLM이 검토한다.

가짜 LLM은 역할(제안·검토·고침·추출·묶기)마다 정해 둔 답을 한다. 일부러 틀린 답을 섞어 **검토 단계가 그것을
잡는지**를 본다: 겹친 개념(스키마), 금액 열을 협력사로 둔 대응표(양식), 원문에 없는 근거 문장(추출), 다른
대상을 같다고 한 묶음(묶기).
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import docsearch, semantic

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"

GOOD_SCHEMA = {
    "classes": [{"name": "협력사", "description": "물품을 공급하는 회사", "examples": ["에이전자"]},
                {"name": "품목", "description": "공급 대상 물품", "examples": ["배터리"]},
                {"name": "부서", "description": "사내 조직", "examples": []}],
    "relations": [{"name": "공급한다", "from": "협력사", "to": "품목", "description": ""},
                  {"name": "담당한다", "from": "부서", "to": "품목", "description": ""}],
}
FORM = ["업체명", "품목", "금액"]


class FakeLlm:
    def __init__(self):
        self.calls: list[str] = []
        self.known: list[str] = []      # 관계 찾기마다 받은 「이미 정한 관계」
        self.review_wrong_all = False
        self.merge_groups = [{"canonical": "에이전자", "names": ["A-Electronics"], "reason": "영문 표기"},
                             {"canonical": "비상사", "names": ["에이전자"], "reason": "둘 다 견적서에 나온다"}]

    def __call__(self, provider, messages, db=None, *a, **kw):
        system, user = messages[0]["content"], messages[1]["content"]
        role = self.role(system)
        self.calls.append(role)
        return "```json\n" + json.dumps(self.answer(role, user), ensure_ascii=False) + "\n```"

    @staticmethod
    def role(system: str) -> str:
        for key, role in (("개념 종류를 설계", "schema"), ("정한 개념 종류를 검토", "schema_review"),
                          ("검토 의견을 반영해 개념 종류", "schema_revise"),
                          ("기준 개념에서 나가는", "relations"), ("찾은 관계 종류를 검토", "relations_review"),
                          ("양식 표의 열을", "map"),
                          ("열 대응표를", "map_review"), ("검토 의견으로 대응표", "map_revise"),
                          ("**내용**에서 개념과 관계를 뽑는다", "extract"), ("뽑은 개념·관계를 검토", "doc_review"),
                          ("같은 대상**을 가리키는 이름들을 묶는다", "merge"), ("같은 대상으로 묶자고", "merge_review")):
            if key in system:
                return role
        raise AssertionError(f"모르는 역할: {system[:80]}")

    def answer(self, role: str, user: str):
        if role == "schema":   # 일부러 겹친 개념 — 검토가 잡아야 한다
            return {"classes": GOOD_SCHEMA["classes"] + [
                {"name": "공급업체", "description": "협력사와 같다", "examples": []}]}
        if role == "schema_review":
            return {"verdict": "revise", "issues": [{"target": "공급업체", "problem": "협력사와 겹친다",
                                                     "suggestion": "협력사로 합친다"}]}
        if role == "schema_revise":
            return {"classes": GOOD_SCHEMA["classes"]}
        if role == "relations":   # 기준 개념에서 나가는 것만 — 목록 밖 개념으로 가는 것은 버려져야 한다
            anchors = json.loads(user[user.index("기준 개념:") + 6:].split(chr(10))[0])
            self.known.append(user[user.index("이미 정한 관계:"):].split(chr(10))[0])
            return {"relations": [r for r in GOOD_SCHEMA["relations"] if r["from"] in anchors]
                    + [{"name": "속한다", "from": anchors[0], "to": "없는개념"}]}
        if role == "relations_review":   # 고친 목록 전체를 낸다 — 여기서는 그대로
            found = json.loads(user[user.index("검토할 관계"):].split(chr(10), 1)[1])
            return {"issues": [], "relations": found}
        if role == "map":       # 일부러 틀린 대응 — 금액 열을 협력사로
            return {"columns": {"업체명": "협력사", "품목": "품목", "금액": "협력사"},
                    "relations": [{"from": "업체명", "rel": "공급한다", "to": "품목"}]}
        if role == "map_review":
            assert "1,200,000" in user          # 실제 값 표본과 대조한다
            return {"verdict": "revise", "issues": [{"column": "금액", "problem": "값이 금액이다", "suggestion": "null"}]}
        if role == "map_revise":
            return {"columns": {"업체명": "협력사", "품목": "품목", "금액": None},
                    "relations": [{"from": "업체명", "rel": "공급한다", "to": "품목"}]}
        if role == "extract" and "A-Electronics" in user:
            sentence = "A-Electronics는 배터리를 공급한다."
            return {"entities": [{"class": "협력사", "name": "A-Electronics", "evidence": sentence}],
                    "relations": [{"from": {"class": "협력사", "name": "A-Electronics"}, "rel": "공급한다",
                                   "to": {"class": "품목", "name": "배터리"}, "evidence": sentence}],
                    "outside": []}
        if role == "extract":
            return {
                "entities": [
                    {"class": "협력사", "name": "에이전자", "evidence": "에이전자는 배터리를 공급한다."},
                    {"class": "협력사", "name": "유령상사", "evidence": "유령상사는 원문에 없는 문장이다."},   # 근거가 원문에 없음
                    {"class": "거래처", "name": "에이전자", "evidence": "에이전자는 배터리를 공급한다."},      # 스키마에 없는 개념
                ],
                "relations": [{"from": {"class": "협력사", "name": "에이전자"}, "rel": "공급한다",
                               "to": {"class": "품목", "name": "배터리"}, "evidence": "에이전자는 배터리를 공급한다."}],
                "outside": [{"name": "납기", "note": "스키마에 없다"}],
            }
        if role == "doc_review":
            items = json.loads(user[user.index("항목:") + 3:])
            return {"wrong": [{"i": it["i"], "problem": "x"} for it in items] if self.review_wrong_all else []}
        if role == "merge":
            return {"groups": self.merge_groups}
        if role == "merge_review":
            return {"same": "A-Electronics" in user, "reason": "영문 표기는 같다" if "A-Electronics" in user else "다른 회사"}
        raise AssertionError(role)


@pytest.fixture
def store(monkeypatch, tmp_path, fresh_settings):
    root = tmp_path / "docs"
    root.mkdir()
    monkeypatch.setenv("PAAS_DOC_ROOTS", str(root))
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("PAAS_DOC_READY_DIR", str(tmp_path / "ready"))
    get_settings.cache_clear()
    from app.db import Base, SessionLocal, engine
    from app.models import DocumentStore, DocumentStoreRegistry, LlmProvider
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.query(DocumentStore).delete()
        db.query(DocumentStoreRegistry).delete()
        db.query(LlmProvider).delete()
        db.add(LlmProvider(name="sem-default", kind="openai", base_url="http://x", model="m", is_default=True))
        db.commit()
    create_app()
    (root / "공급계약.md").write_text(
        "# 공급 계약\n\n에이전자는 배터리를 공급한다.\n\n구매팀이 배터리를 담당한다.\n", encoding="utf-8")
    (root / "영문메모.md").write_text("# 메모\n\nA-Electronics는 배터리를 공급한다.\n", encoding="utf-8")
    for i in range(semantic.FORM_MIN_DOCS):
        (root / f"견적서{i}.md").write_text(
            f"# 견적서 {i}\n\n| 업체명 | 품목 | 금액 |\n|---|---|---|\n"
            f"| 에이전자(주) | 배터리 | 1,200,000 |\n| 비상사 | 케이블 | 30,000 |\n", encoding="utf-8")
    docsearch.reindex("docs", root)
    fake = FakeLlm()
    monkeypatch.setattr(semantic.llm_service, "chat_completion", fake)
    return fake


def _db():
    from app.db import SessionLocal
    return SessionLocal()


def _confirm_schema_and_form():
    with _db() as db:
        draft = semantic.propose_schema(db, "docs")
        semantic.confirm_schema("docs", draft["revised"], "admin")
        form = semantic.propose_mapping(db, "docs", " | ".join(FORM))
        semantic.confirm_mapping("docs", " | ".join(FORM), form["draft"]["revised"], "admin")


def test_schema_proposal_is_reviewed_and_revised_before_the_admin_confirms(store):
    with _db() as db:
        draft = semantic.propose_schema(db, "docs")
    # 개념 종류(제안·검토·고침) 뒤 관계는 묶음마다 찾고 검토한다(개념 3개 = 한 묶음)
    assert store.calls == ["schema", "schema_review", "schema_revise", "relations", "relations_review"]
    assert "공급업체" in [c["name"] for c in draft["proposal"]["classes"]]       # 원안·검토·고친 안을 함께 남긴다
    assert draft["review"]["issues"][0]["target"] == "공급업체"
    assert [c["name"] for c in draft["revised"]["classes"]] == ["협력사", "품목", "부서"]
    assert [r["name"] for r in draft["revised"]["relations"]] == ["공급한다", "담당한다"]   # 목록 밖으로 가는 것은 버림
    assert draft["problems"] == []
    # 표본은 양식을 하나만 — 견적서 다섯 건이 표본을 다 먹지 않는다
    assert sum(p.startswith("견적서") for p in draft["samples"]) == 1
    assert semantic.status("docs")["schema"] is None                              # 확정은 관리자가
    with pytest.raises(semantic.SemanticError, match="from/to"):
        semantic.confirm_schema("docs", {**GOOD_SCHEMA, "relations": [{"name": "x", "from": "없음", "to": "품목"}]}, "a")
    confirmed = semantic.confirm_schema("docs", draft["revised"], "admin")
    assert confirmed["version"] and semantic.status("docs")["schema"]["version"] == confirmed["version"]


def test_form_mapping_is_checked_against_real_values_and_then_applied_by_rules(store):
    _confirm_schema_and_form()
    form = next(f for f in semantic.forms("docs") if f["name"] == " | ".join(FORM))
    assert form["documents"] == semantic.FORM_MIN_DOCS
    assert form["draft"]["proposal"]["columns"]["금액"] == "협력사"           # 원안의 실수
    assert form["confirmed"]["columns"]["금액"] is None                        # 실제 값과 대조한 검토가 고쳤다
    store.calls.clear()
    with _db() as db:
        result = semantic.analyze(db, "docs")
    assert result["form_docs"] == semantic.FORM_MIN_DOCS
    assert store.calls.count("extract") == 2            # 양식 문서는 LLM이 읽지 않는다 — 나머지 두 문서만
    rel = semantic.neighbors("docs", "품목", "케이블")["relations"]
    assert rel and rel[0]["name"] == "비상사" and rel[0]["evidence"][0]["origin"] == "form"
    assert len({e["path"] for r in semantic.neighbors("docs", "품목", "배터리")["relations"]
                for e in r["evidence"]}) >= 3


def test_extraction_keeps_only_what_the_document_says(store, monkeypatch):
    """기계 검사 — 원문에 없는 근거, 스키마에 없는 개념은 버린다. 표본 검토는 틀렸다고 한 항목을 뺀다."""
    monkeypatch.setattr(semantic, "REVIEW_EVERY", 1)            # 모두 표본으로
    _confirm_schema_and_form()
    with _db() as db:
        result = semantic.analyze(db, "docs")
    assert result["analyzed"] == 2 + semantic.FORM_MIN_DOCS and result["failed"] == 0
    assert result["dropped"] >= 2                               # 유령상사(원문에 없는 근거)·거래처(스키마 밖)
    names = {n["name"] for n in semantic.search("docs", "협력사")}
    assert "유령상사" not in names and "에이전자" in names
    assert semantic.status("docs")["outside"]["납기"] >= 1       # 스키마 밖 후보는 모아 보여 준다
    assert store.calls.count("doc_review") == 2


def test_unchanged_documents_are_not_sent_again_but_a_new_schema_version_is(store):
    _confirm_schema_and_form()
    with _db() as db:
        semantic.analyze(db, "docs")
        store.calls.clear()
        again = semantic.analyze(db, "docs")
    assert again["analyzed"] == 0 and store.calls == []
    semantic.confirm_schema("docs", {**GOOD_SCHEMA, "classes": GOOD_SCHEMA["classes"] + [
        {"name": "규정", "description": "", "examples": []}]}, "admin")
    with _db() as db:
        redo = semantic.analyze(db, "docs")
    # 판이 바뀌면 문서는 다시 — 양식 대응표는 옛 판에 맞춘 것이라 쓰지 않고 그 문서들도 LLM이 읽는다
    assert redo["analyzed"] == 2 + semantic.FORM_MIN_DOCS and redo["form_docs"] == 0


def test_a_high_sample_error_rate_pauses_the_store(store, monkeypatch):
    monkeypatch.setattr(semantic, "REVIEW_EVERY", 1)
    monkeypatch.setattr(semantic, "STOP_MIN_ITEMS", 2)
    store.review_wrong_all = True
    with _db() as db:
        semantic.confirm_schema("docs", GOOD_SCHEMA, "admin")
        first = semantic.analyze(db, "docs")
        assert first["paused"] and "표본 검토" in first["paused"]
        notes = semantic.status("docs")["review"]["notes"]   # 무엇을 왜 틀렸다고 했는지 — 멈춤의 근거
        assert notes and notes[0]["problem"] == "x" and notes[0]["evidence"] and notes[0]["path"]
        store.calls.clear()
        assert semantic.analyze(db, "docs")["paused"] and store.calls == []    # 멈춘 동안 부르지 않는다
    semantic.resume("docs")
    status = semantic.status("docs")
    assert status["paused"] is None and status["review"]["items"] == 0


def test_merging_reviews_every_group_and_can_be_undone(store):
    _confirm_schema_and_form()
    with _db() as db:
        semantic.analyze(db, "docs")
        result = semantic.propose_merges(db, "docs")
    applied = {(a["alias"], a["canonical"]) for a in result["applied"]}
    assert ("에이전자(주)", "에이전자") in applied or ("에이전자", "에이전자(주)") in applied   # 정규화 — 검토 없이
    assert ("A-Electronics", "에이전자") in applied                                          # LLM 제안 → 검토 통과
    assert [r["canonical"] for r in result["rejected"]] == ["비상사"]                         # 검토가 거절
    hit = next(n for n in semantic.search("docs", "협력사", "electronics"))
    assert "A-Electronics" in hit["aliases"] or hit["name"] == "A-Electronics"
    semantic.remove_alias("docs", "협력사", "A-Electronics")
    assert any(n["name"] == "A-Electronics" for n in semantic.search("docs", "협력사"))


def test_an_admin_uses_the_default_provider_of_any_organization(store):
    """내용 분석은 관리자만 시작한다 — 관리자는 모든 조직의 프로바이더를 쓴다."""
    from app.models import LlmProvider, Organization
    with _db() as db:
        org = Organization(name="sem-other")
        db.add(org)
        db.commit()
        db.query(LlmProvider).update({"organization_id": org.id})
        db.commit()
        assert semantic.provider_for(db, "docs").name == "sem-default"


def test_api_runs_proposals_in_the_background_and_guards_admin_actions(store):
    c = TestClient(create_app())
    member = {"x-api-key": c.post(f"{API}/keys", json={"name": "sem-dev"}, headers=ADMIN).json()["key"]}
    url = f"{API}/ontology/stores/docs/semantic"
    assert c.post(f"{url}/schema/propose", headers=member).status_code == 403
    assert c.post(f"{url}/analyze", headers=ADMIN).status_code == 409          # 스키마가 없으면 시작하지 않는다
    assert c.post(f"{url}/schema/propose", headers=ADMIN).status_code == 202
    for _ in range(100):
        status = c.get(url, headers=ADMIN).json()
        if status["task"].get("done"):
            break
        time.sleep(0.05)
    assert status["task"]["error"] is None and status["schema_draft"]["revised"]
    assert c.put(f"{url}/schema", json=status["schema_draft"]["revised"], headers=ADMIN).status_code == 200
    assert c.post(f"{url}/analyze", headers=ADMIN).status_code == 200
    for _ in range(100):
        if not c.get(url, headers=ADMIN).json()["running"]:
            break
        time.sleep(0.05)
    found = c.get(f"{url}/search", params={"cls": "협력사"}, headers=ADMIN).json()["nodes"]
    assert any(n["name"] == "에이전자" for n in found)
    near = c.get(f"{url}/neighbors", params={"cls": "협력사", "name": "에이전자"}, headers=ADMIN).json()
    assert near["relations"][0]["rel"] == "공급한다" and near["relations"][0]["evidence"][0]["text"]


def test_mcp_graph_server_serves_the_content_graph(store):
    """MCP(그리고 통합 서버를 거쳐 스마트워크)가 내용 그래프를 찾고, 관계마다 근거 문장을 받는다."""
    from app.api import mcp_servers
    with pytest.raises(mcp_servers.mcp_server.McpToolError, match="내용 그래프가 없습니다"):
        mcp_servers._graph_call("concept_schema", {"source": "docs"})
    _confirm_schema_and_form()
    with _db() as db:
        semantic.analyze(db, "docs")
    schema = json.loads(mcp_servers._graph_call("concept_schema", {"source": "docs"}))
    assert [c["name"] for c in schema["docs"]["classes"]] == ["협력사", "품목", "부서"]
    found = json.loads(mcp_servers._graph_call("find_concepts", {"source": "docs", "cls": "품목", "q": "배터"}))
    assert found[0]["name"] == "배터리"
    rel = json.loads(mcp_servers._graph_call("concept_relations", {"source": "docs", "cls": "품목", "name": "배터리"}))
    assert rel[0]["relations"][0]["evidence"][0]["path"]


def test_alias_chains_resolve_to_one_name(store):
    """묶기를 여러 번 돌면 별칭이 사슬이 된다(A-Electronics → 에이전자 → 에이전자(주)) — 끝까지 따라가 한 대상으로 본다."""
    _confirm_schema_and_form()
    with _db() as db:
        semantic.analyze(db, "docs")
    conn = semantic._conn("docs")
    try:
        conn.executemany("INSERT INTO sem_aliases (cls, alias, canonical) VALUES ('협력사', ?, ?)",
                         [("A-Electronics", "에이전자"), ("에이전자", "에이전자(주)")])
        conn.commit()
        assert semantic.canonical_map(conn)[("협력사", "A-Electronics")] == "에이전자(주)"
    finally:
        conn.close()
    hit = [n for n in semantic.search("docs", "협력사") if "에이전자" in n["name"] or "Electronics" in n["name"]]
    assert len(hit) == 1 and hit[0]["name"] == "에이전자(주)"
    assert set(hit[0]["aliases"]) == {"에이전자", "A-Electronics"}
    rel = semantic.neighbors("docs", "협력사", "에이전자(주)")["relations"]
    assert {e["path"] for r in rel for e in r["evidence"]} >= {"공급계약.md", "영문메모.md", "견적서0.md"}


def test_pdf_line_breaks_and_abstract_names_are_kept_for_review_not_dropped():
    """실측(신용평가서): PDF가 낱말 가운데서 줄을 바꾸고(`다\n만`), 재무위험 같은 개념은 이름이 문장의 요약이다.
    예전 검사는 둘 다 버려 그 문서의 관계가 통째로 빠졌다. 지금은 근거가 원문에 있으면 남기고 검토로 보낸다."""
    schema = {"classes": [{"name": "신용평가"}, {"name": "재무위험"}],
              "relations": [{"name": "지적한다", "from": "신용평가", "to": "재무위험"}]}
    text = "■ 등급평가의견\n여신 리스크 높지 않음. 다\n만, 2024년 전년대비 매출외형 감소한 가운데 수익성 약화되었음."
    sentence = "다만, 2024년 전년대비 매출외형 감소한 가운데 수익성 약화되었음."
    data = {"entities": [{"class": "재무위험", "name": "매출 외형 감소", "evidence": sentence},
                         {"class": "재무위험", "name": "지어낸 위험", "evidence": "원문에 없는 문장이다."}],
            "relations": [{"from": {"class": "신용평가", "name": "기업신용평가"}, "rel": "지적한다",
                           "to": {"class": "재무위험", "name": "매출 외형 감소"}, "evidence": sentence}]}
    nodes, edges, dropped, unanchored = semantic.check_extraction(data, schema, text)
    assert [n["name"] for n in nodes] == ["매출 외형 감소"] and len(edges) == 1
    assert dropped == 1                 # 지어낸 근거는 여전히 버린다
    # 「매출 외형 감소」는 공백을 빼면 근거 안에 있다. 관계의 「기업신용평가」는 문장에 없다 — 남기되 검토 대상
    assert unanchored == 1


def test_documents_with_unanchored_items_are_always_reviewed(store, monkeypatch):
    monkeypatch.setattr(semantic, "REVIEW_EVERY", 10_000)          # 표본으로는 아무것도 뽑히지 않게
    monkeypatch.setattr(semantic, "_sampled", lambda path: False)
    real = store.answer

    def abstract(role, user):
        out = real(role, user)
        if role == "extract" and "에이전자는" in user:
            out["entities"].append({"class": "품목", "name": "전지류", "evidence": "에이전자는 배터리를 공급한다."})
        return out

    store.answer = abstract
    semantic.confirm_schema("docs", GOOD_SCHEMA, "admin")
    with _db() as db:
        semantic.analyze(db, "docs")
    assert store.calls.count("doc_review") == 1     # 「전지류」가 있는 문서만 — 이름이 근거에 없다


def test_a_report_with_a_form_table_still_gets_its_prose_read(store, tmp_path):
    """실측(신용평가서): 양식 표 하나를 품었다는 이유로 문서 전체가 「양식 문서」가 되어 LLM을 건너뛰었고, 본문의
    관계가 통째로 빠졌다. 표 밖 글이 충분하면 표는 규칙으로, 나머지 글은 LLM으로 읽는다."""
    root = get_settings().doc_roots.split("=")[-1] if "=" in get_settings().doc_roots else get_settings().doc_roots
    from pathlib import Path
    prose = "에이전자는 배터리를 공급한다. " + "이 보고서는 협력사의 재무와 거래 현황을 설명한다. " * 100
    (Path(root) / "신용평가보고서.md").write_text(
        f"# 신용평가 보고서\n\n{prose}\n\n| 업체명 | 품목 | 금액 |\n|---|---|---|\n| 비상사 | 케이블 | 30,000 |\n",
        encoding="utf-8")
    docsearch.reindex("docs", Path(root))
    _confirm_schema_and_form()
    store.calls.clear()
    with _db() as db:
        result = semantic.analyze(db, "docs")
    assert result["form_docs"] == semantic.FORM_MIN_DOCS          # 견적서만 규칙으로만
    assert store.calls.count("extract") == 3                       # 보고서는 LLM이 본문을 읽는다
    conn = semantic._conn("docs")
    try:
        origins = {(r["origin"], r["name"]) for r in conn.execute(
            "SELECT origin, name FROM sem_nodes WHERE path = '신용평가보고서.md'")}
    finally:
        conn.close()
    assert ("form", "비상사") in origins and ("llm", "에이전자") in origins
    # LLM에는 양식 표를 다시 보이지 않는다
    assert "| 비상사 |" not in semantic.strip_forms(open(Path(root) / "신용평가보고서.md", encoding="utf-8").read(),
                                                   [FORM])


def test_a_raw_line_break_inside_a_json_string_is_not_a_failure():
    """실측: 모델이 근거 문장을 옮기다 문자열 안에 줄바꿈을 그대로 넣었다 — 엄밀한 JSON이 아니어서 문서가 실패했다."""
    broken = '{"entities":[{"class":"협력사","name":"A","evidence":"1. 기업신용평가 요약' + chr(10) + '- 발행번호"}]}'
    assert semantic._json_of(broken)["entities"][0]["evidence"] == "1. 기업신용평가 요약" + chr(10) + "- 발행번호"


def test_relations_are_found_batch_by_batch_without_a_cap(store, monkeypatch):
    """관계는 한 번에 다 찾지 않는다 — 개념을 묶어 차례로 찾고 묶음마다 검토한다. 상한은 없다(실측: 한 번에 내게 하니
    40개 상한을 넘어 확정할 수 없었다). 뒤 묶음은 앞에서 정한 관계를 받아 같은 이름을 쓴다. 진행은 단계마다 보고한다."""
    monkeypatch.setattr(semantic, "RELATION_BATCH", 1)          # 개념 하나씩 = 세 묶음
    steps: list[tuple] = []
    with _db() as db:
        draft = semantic.propose_schema(db, "docs", report=lambda text, step=None, total=None: steps.append((step, total)))
    # 품목에서 나가는 관계는 없다 — 찾을 것이 없으면 검토를 부르지 않는다
    assert store.calls == ["schema", "schema_review", "schema_revise",
                           "relations", "relations_review", "relations", "relations", "relations_review"]
    assert draft["batches"] == 3 and [r["name"] for r in draft["revised"]["relations"]] == ["공급한다", "담당한다"]
    assert "공급한다" not in store.known[0] and "공급한다" in store.known[1] and "공급한다" in store.known[2]
    assert [s[0] for s in steps][:4] == [1, 2, 3, 4] and steps[-1] == (10, 10)   # 4 + 묶음마다 2단계(찾기·검토)
    many = {"classes": [{"name": f"개념{i}"} for i in range(60)],
            "relations": [{"name": f"관계{i}", "from": "개념0", "to": f"개념{i % 60}"} for i in range(120)]}
    assert semantic.check_schema(many) == []                    # 개수 상한이 없다
    semantic.confirm_schema("docs", draft["revised"], "admin")
