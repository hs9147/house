"""내용 온톨로지(services/semantic) — 제안은 LLM이 검토하고, 추출은 기계로 대조하고, 표본은 LLM이 검토한다.

가짜 LLM은 역할(제안·검토·고침·추출·묶기)마다 정해 둔 답을 한다. 일부러 틀린 답을 섞어 **검토 단계가 그것을
잡는지**를 본다: 겹친 개념(스키마), 금액 열을 협력사로 둔 대응표(양식), 원문에 없는 근거 문장(추출), 다른
대상을 같다고 한 짝(묶기 — 기준으로 정하지 못한 짝만 LLM에 간다).
"""
import json
import time
from pathlib import Path

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
                          ("같은 대상**인지 판정", "merge"), ("같은 대상이라고 했다", "merge_review"),
                          ("다음부터 규칙으로 처리할 수 있는", "merge_rules"),
                          ("검증 결과로 규칙을 고친다", "template_revise"), ("**정규식 규칙**을 짠다", "template")):
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
        if role == "extract" and "GP 협력사 등록 품의" in user:     # 템플릿 문서 — 라벨 다음 줄이 값
            lines = [line for line in user.splitlines() if line.strip()]
            value = {k: lines[lines.index(k) + 1] for k in ("기업명", "거래대상", "구매요청 부서") if k in lines}
            co, item, dept = value.get("기업명"), value["거래대상"], value["구매요청 부서"]
            return {"entities": [{"class": "협력사", "name": co, "evidence": f"기업명 {co}"},
                                 {"class": "품목", "name": item, "evidence": f"거래대상 {item}"},
                                 {"class": "부서", "name": dept, "evidence": f"구매요청 부서 {dept}"}] if co else [],
                    "relations": ([{"from": {"class": "협력사", "name": co}, "rel": "공급한다",
                                    "to": {"class": "품목", "name": item}, "evidence": f"거래대상 {item}"}] if co else [])
                    + [{"from": {"class": "부서", "name": dept}, "rel": "담당한다",
                        "to": {"class": "품목", "name": item}, "evidence": f"거래대상 {item}"}],
                    "outside": []}
        if role == "template":       # 처음 규칙은 품목을 아무 줄에서나 잡는다(일부러 틀림) — 검증이 잡아야 한다
            return {"fields": [{"class": "협력사", "pattern": r"^기업명\n(.+)$", "required": True},
                               {"class": "품목", "pattern": r"^(.+전지)$"}],
                    "relations": [{"from": 0, "rel": "공급한다", "to": 1}]}
        if role == "template_revise":
            assert '"extra"' in user and "missing_required" in user     # 검증 결과를 받아 고친다
            return {"fields": [{"class": "협력사", "pattern": r"^기업명\n(.+)$", "required": True},
                               {"class": "품목", "pattern": r"^거래대상\n(.+)$"},
                               {"class": "부서", "pattern": r"^구매요청 부서\n(.+)$"}],
                    "relations": [{"from": 0, "rel": "공급한다", "to": 1}, {"from": 2, "rel": "담당한다", "to": 1}]}
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
        if role == "merge":          # 영문 표기 짝은 모두 같다고 한다 — 비상사까지(일부러 틀림)
            pairs = json.loads(user)
            return {"pairs": [{"i": p["i"], "same": "A-Electronics" in (p["a"], p["b"]), "reason": "영문 표기"}
                              for p in pairs]}
        if role == "merge_review":   # 검토가 비상사를 잡는다
            pairs = json.loads(user)
            return {"pairs": [{"i": p["i"], "same": "비상사" not in (p["a"], p["b"]),
                               "reason": "다른 회사" if "비상사" in (p["a"], p["b"]) else "영문 표기는 같다"}
                              for p in pairs]}
        if role == "merge_rules":
            return {"translit": {"a": "에이", "electronics": "전자", "lg": "엘지"}, "strip": ["(주)"]}
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
    monkeypatch.setattr(semantic, "MIN_CHARS", 0)      # 시험 문서는 짧다 — 「읽을 글이 거의 없다」로 건너뛰지 않게
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


def test_merging_decides_by_rules_and_asks_the_llm_only_the_unclear_pairs(store):
    """협력사 이름 넷(에이전자·에이전자(주)·A-Electronics·비상사) = 짝 여섯. 정규화가 같은 짝은 기준으로 묶고, 글자가 전혀
    다른 한글끼리는 기준으로 거절한다. 한글·영문 짝(점수가 말해 주지 못한다)만 LLM이 한 번에 판정하고 검토한다."""
    _confirm_schema_and_form()
    with _db() as db:
        semantic.analyze(db, "docs")
        store.calls.clear()
        result = semantic.propose_merges(db, "docs")
    assert store.calls == ["merge", "merge_review", "merge_rules"]           # 짝마다가 아니라 묶음 하나로
    s = result["stats"]
    assert (s["pairs"], s["rule"], s["gray"], s["llm"]) == (7, 4, 3, 3)       # 품목(배터리·케이블) 짝 하나 더
    applied = {a["alias"]: a["canonical"] for a in result["applied"]}
    head = "에이전자(주)"                                     # 더 많은 문서에 나온 이름이 대표
    assert applied == {"에이전자": head, "A-Electronics": head}
    assert [(r["a"], r["b"]) for r in result["rejected"]] == [("A-Electronics", "비상사")]   # 판정 → 검토가 거절
    hit = next(n for n in semantic.search("docs", "협력사", "electronics"))
    assert hit["name"] == head and "A-Electronics" in hit["aliases"]
    status = semantic.status("docs")
    assert status["merge_rules_draft"]["translit"] == {"a": "에이", "electronics": "전자"}   # 이미 있는 lg·(주)는 뺀다
    assert status["merge_rules_draft"]["strip"] == []
    assert status["pairs"] == {"rule:same": 1, "rule:different": 3, "llm:same": 2, "llm:different": 1}


def test_undoing_a_merge_is_remembered_and_nothing_is_asked_twice(store):
    _confirm_schema_and_form()
    with _db() as db:
        semantic.analyze(db, "docs")
        semantic.propose_merges(db, "docs")
        semantic.remove_alias("docs", "협력사", "A-Electronics", "admin")
        assert any(n["name"] == "A-Electronics" for n in semantic.search("docs", "협력사"))
        store.calls.clear()
        again = semantic.propose_merges(db, "docs")
    assert store.calls == []                                   # 판정한 짝은 다시 묻지 않는다
    assert again["stats"]["cached"] == 2 and again["stats"]["human"] == 1
    # LLM은 A-Electronics = 에이전자라고 했지만, 사람이 A-Electronics ≠ 에이전자(주)라 했고 에이전자 = 에이전자(주)는
    # 기준이 정했다 — 셋이 한 묶음일 수는 없으니 덜 믿을 LLM 판정을 버린다
    aliases = {a["alias"]: a["canonical"] for a in semantic.status("docs")["aliases"]}
    assert aliases == {"에이전자": "에이전자(주)"}


def test_same_identifier_merges_and_different_identifiers_never_do():
    """근거에 같은 협력사코드가 있으면 이름이 달라도 같은 대상(실측: KR029375 = (주)엘지씨엔에스 = LG CNS Co., Ltd.)."""
    from app.services import semantic_merge as sm
    rules = sm.DEFAULT_RULES
    ids = {"(주)엘지씨엔에스": sm.ids_of(["| KR-CT220100008 | (주)엘지씨엔에스 | KR029375 |"], rules),
           "엘지 정보통신": sm.ids_of(["협력사코드 KR029375"], rules),
           "에이전자": {"KR000001"}, "에이전자(주)": {"KR000002"}}
    assert sm.decide("(주)엘지씨엔에스", "엘지 정보통신", rules, ids)[:2] == ("same", "id")
    assert sm.decide("에이전자", "에이전자(주)", rules, ids)[:2] == ("different", "id")   # 이름이 같아도 코드가 다르다
    assert sm.decide("LG CNS Co., Ltd.", "(주)엘지씨엔에스", rules, {})[:2] == ("same", "rule")   # 음역 정규화
    assert sm.decide("GS네오텍(주)", "지에스네오텍", rules, {})[0] == "same"                    # 낱말 안의 로마자도
    assert sm.decide("삼성전자", "삼성전기", rules, {})[0] == "different"


def test_merge_rules_are_confirmed_by_an_admin_and_checked(store):
    with pytest.raises(semantic.SemanticError):
        semantic.confirm_merge_rules("docs", {"id_patterns": ["[A-Z"]}, "admin")
    with pytest.raises(semantic.SemanticError):
        semantic.confirm_merge_rules("docs", {"thresholds": {"merge": 0.3, "reject": 0.5}}, "admin")
    before = semantic.status("docs")["merge_rules"]
    after = semantic.confirm_merge_rules("docs", {"translit": {**before["translit"], "electronics": "전자"}}, "admin")
    assert after["version"] != before["version"] and after["translit"]["electronics"] == "전자"
    assert after["weights"] == before["weights"]                 # 보낸 것만 바뀐다
    assert semantic.status("docs")["merge_rules"]["version"] == after["version"]


def _registration_docs(root: Path) -> None:
    """같은 틀의 「등록 품의」 — 라벨 줄은 같고 값만 다르다(실측: 신규 협력사 등록 pptx). 마지막 것은 기업명 라벨이
    다르다(틀이 조금 다른 문서) — 필수 필드를 못 찾아 LLM으로 넘어가야 한다."""
    rows = [("가나전지", "리튬전지", "구매1팀"), ("다라전지", "납축전지", "구매2팀"), ("마바전지", "니켈전지", "구매1팀"),
            ("사아전지", "건전지", "구매3팀"), ("자차전지", "태양전지", "구매2팀"), ("카타전지", "연료전지", "구매1팀"),
            ("파하전지", "수소전지", "구매3팀")]
    for i, (co, item, dept) in enumerate(rows):
        label = "기업명" if i < len(rows) - 1 else "업체 이름"
        (root / f"등록품의_{i}.md").write_text(
            f"GP 협력사 등록 품의\n거래선 개요\n{label}\n{co}\n구매요청 부서\n{dept}\n거래대상\n{item}\n신규 등록 사유\n"
            f"경쟁입찰을 통해 우선협상자로 선정\n평가 방식\n현장 or 서면 평가\n첨부. 사업자등록증 사본\nLGE Internal Use Only\n",
            encoding="utf-8")


def test_template_documents_get_rules_checked_on_other_documents(store, tmp_path, monkeypatch):
    """③′ 같은 틀의 문서 여섯은 LLM이 짠 규칙으로 뽑는다. 규칙은 세 건(정답: LLM 추출)으로 짜고 **다른** 세 건으로
    검증한다 — 처음 규칙이 틀려 정밀도가 모자라면 검증 결과로 한 번 고친다. 확정 뒤 분석은 그 문서들에 LLM 추출을
    부르지 않고, 필수 필드가 없는 문서(틀이 다르다)만 LLM으로 넘긴다."""
    monkeypatch.setattr(semantic, "REVIEW_EVERY", 10 ** 9)     # 표본 검토는 따로 본다
    _confirm_schema_and_form()
    _registration_docs(tmp_path / "docs")
    docsearch.reindex("docs", tmp_path / "docs")
    found = semantic.templates("docs")
    assert len(found) == 1 and found[0]["documents"] == 7 and "기업명" in found[0]["skeleton"]
    tid = found[0]["id"]
    store.calls.clear()
    with _db() as db:
        draft = semantic.propose_template(db, "docs", tid)
    assert store.calls == ["extract"] * 6 + ["template", "template_revise"]
    assert draft["first"]["precision"] < semantic.TEMPLATE_PRECISION              # 처음 규칙 — 검증이 잡았다
    assert draft["evaluation"]["precision"] == 1.0 and draft["evaluation"]["recall"] == 1.0
    assert set(draft["train"]).isdisjoint(c["path"] for c in draft["check"])     # 짠 문서와 잰 문서가 다르다
    with pytest.raises(semantic.SemanticError, match="정밀도"):
        semantic.confirm_template("docs", tid, draft["proposal"], "admin")       # 고치기 전 규칙은 확정되지 않는다
    confirmed = semantic.confirm_template("docs", tid, draft["revised"], "admin")
    assert confirmed["precision"] == 1.0 and semantic.templates("docs")[0]["confirmed"]["version"]

    store.calls.clear()
    with _db() as db:
        result = semantic.analyze(db, "docs")
    assert result["template_docs"] == 6 and result["escalated"] == 1
    assert store.calls.count("extract") == 3        # 공급계약·영문메모·틀이 다른 등록품의 하나
    near = semantic.neighbors("docs", "협력사", "마바전지")
    assert {(r["rel"], r["name"]) for r in near["relations"]} == {("공급한다", "니켈전지")}
    assert near["relations"][0]["evidence"][0]["origin"] == "template"
    assert semantic.status("docs")["analyzed"]["template"] == 6
    with _db() as db:                               # 바뀐 것이 없으면 다시 돌지 않는다
        assert semantic.analyze(db, "docs")["analyzed"] == 0


def test_template_rules_are_sampled_for_review_like_llm_extraction(store, tmp_path, monkeypatch):
    """규칙이 뽑은 것도 표본은 LLM이 검토하고 오류율에 센다 — 규칙이 틀리기 시작하면 멈춘다."""
    _confirm_schema_and_form()
    _registration_docs(tmp_path / "docs")
    docsearch.reindex("docs", tmp_path / "docs")
    tid = semantic.templates("docs")[0]["id"]
    with _db() as db:
        draft = semantic.propose_template(db, "docs", tid)
    semantic.confirm_template("docs", tid, draft["revised"], "admin")
    monkeypatch.setattr(semantic, "REVIEW_EVERY", 1)
    store.review_wrong_all = True
    with _db() as db:
        semantic.analyze(db, "docs")
    status = semantic.status("docs")
    assert any(n["path"].startswith("등록품의_") for n in status["review"]["notes"])
    assert status["paused"]


def test_human_labels_are_queued_measured_and_used_at_once(store):
    """⑤ 사람 정답 — 짝은 「같은 대상인가」, 개체는 「맞게 뽑았나」. 짝 판정은 바로 사람의 판정으로 남아 다음 묶기에
    쓰이고, 틀린 개체는 그래프에서 빠진다. 확인한 것은 다시 묻지 않는다."""
    _confirm_schema_and_form()
    with _db() as db:
        semantic.analyze(db, "docs")
        semantic.propose_merges(db, "docs")
    queue = semantic.gold_queue("docs", "pair", 50)
    pair = next(q for q in queue if {q["a"], q["b"]} == {"A-Electronics", "에이전자"})
    assert pair["source"] == "llm" and pair["a_evidence"]["evidence"]          # 두 이름의 근거를 함께 보인다
    metrics = semantic.label_gold("docs", "pair", pair["item"], False, "admin")  # LLM은 같다고 했지만 다르다
    assert metrics["pair"]["by"]["llm"] == {"n": 1, "correct": 0, "accuracy": 0.0}
    assert pair["item"] not in {q["item"] for q in semantic.gold_queue("docs", "pair", 50)}
    with _db() as db:
        semantic.propose_merges(db, "docs")
    aliases = {a["alias"]: a["canonical"] for a in semantic.status("docs")["aliases"]}
    assert "A-Electronics" not in aliases                     # 사람의 「다르다」가 LLM의 「같다」를 이긴다

    node = next(q for q in semantic.gold_queue("docs", "node", 50) if q["name"] == "케이블")
    semantic.label_gold("docs", "node", node["item"], False, "admin")
    assert not semantic.search("docs", "품목", "케이블") or all(
        n["documents"] < 5 for n in semantic.search("docs", "품목", "케이블"))
    gold = semantic.status("docs")["gold"]
    assert gold["node"]["by"]["form"]["n"] == 1 and gold["pair"]["labeled"] == 1
    with pytest.raises(semantic.SemanticError):
        semantic.label_gold("docs", "node", "없는\t항목\t이름", True, "admin")


def _pairs_for_tuning():
    """같은 대상(한 글자 오타)과 다른 대상(전혀 다른 이름) 스무 쌍씩."""
    import itertools
    syll = "가나다라마바사아자차카타파하거너더러머버"
    same, diff = {}, {}
    for i in range(20):
        name = syll[i] + syll[(i + 3) % 20] + syll[(i + 7) % 20] + syll[(i + 11) % 20] + "테크"
        typo = name[:2] + "뷁" + name[3:]
        same[(name, typo)] = 1
    names = [k[0] for k in same]
    for a, b in itertools.islice(itertools.combinations(names, 2), 0, 400, 9):
        diff[(a, b)] = 0
    return {**same, **dict(list(diff.items())[:20])}


def test_tuning_promotes_new_weights_only_when_held_out_labels_improve():
    """D — 사람 정답의 70%로 가중치·문턱을 배우고, 남긴 30%에서 지금 기준보다 나을 때만 바꾼다."""
    from app.services import semantic_merge as sm
    from app.services import semantic_tune as tu
    human = _pairs_for_tuning()
    assert len(human) == 40
    blind = {**sm.DEFAULT_RULES, "weights": dict.fromkeys(sm.DEFAULT_RULES["weights"], 0.0),
             "thresholds": {"merge": 0.99, "reject": 0.01}}       # 모든 짝이 0.5 — 전부 LLM에 넘긴다
    first = tu.tune_merge(blind, human, [])
    assert first["status"] == "promoted"
    assert first["champion"]["gray"] == first["champion"]["n"] and first["challenger"]["wrong"] == 0
    assert first["challenger"]["gray"] < first["champion"]["gray"]
    again = tu.tune_merge(first["rules"], human, [])               # 이미 배운 기준 — 더 낫지 않으면 그대로
    assert again["status"] == "kept"
    few = tu.tune_merge(blind, dict(list(human.items())[:5]), [])
    assert few["status"] == "skipped" and "20" in few["reason"]


def test_tuning_suspends_template_rules_that_humans_find_wrong(store, tmp_path, monkeypatch):
    """D — 템플릿 규칙이 뽑은 것을 사람이 틀렸다고 하면(정밀도가 기준 밑) 그 규칙을 멈추고, 그 문서들은 다음 분석에서
    LLM이 읽는다. 묶기 기준은 정답이 모자라 배우지 않는다. 스케줄러가 스키마를 확정한 저장소마다 하루에 한 번 돈다."""
    monkeypatch.setattr(semantic, "REVIEW_EVERY", 10 ** 9)
    _confirm_schema_and_form()
    _registration_docs(tmp_path / "docs")
    docsearch.reindex("docs", tmp_path / "docs")
    tid = semantic.templates("docs")[0]["id"]
    with _db() as db:
        semantic.confirm_template("docs", tid, semantic.propose_template(db, "docs", tid)["revised"], "admin")
        semantic.analyze(db, "docs")
    by_rule = [q for q in semantic.gold_queue("docs", "node", 50) if q["origin"] == "template"]
    assert len(by_rule) >= semantic.TEMPLATE_GOLD_MIN
    for q in by_rule[:semantic.TEMPLATE_GOLD_MIN]:
        semantic.label_gold("docs", "node", q["item"], False, "admin")
    out = semantic.tune("docs", "admin")
    assert out["merge"]["status"] == "skipped"
    assert out["templates"] == [{"id": tid, "status": "suspended", "check_precision": 1.0, "human_precision": 0.0,
                                 "human_labels": semantic.TEMPLATE_GOLD_MIN}]
    t = semantic.templates("docs")[0]
    assert t["confirmed"] is None and "사람 정답 0%" in t["suspended"]["reason"]
    assert semantic.status("docs")["tune_history"][-1]["templates"][0]["status"] == "suspended"
    store.calls.clear()
    with _db() as db:
        semantic.analyze(db, "docs")
    assert store.calls.count("extract") == 7                    # 등록품의 일곱 건 모두 LLM으로

    from app.models import JobKind
    from app.services import scheduler
    with _db() as db:
        assert (JobKind.semantic_tune, "docs") in scheduler._wanted(db)


def test_the_scheduler_check_works_with_a_relative_index_dir(store, tmp_path, monkeypatch):
    """운영 설정의 색인 폴더는 상대 경로다(data/doc-index) — 스케줄러가 tick마다 묻는 has_schema가 여기서 실패하면
    그 tick의 다른 job이 모두 멈춘다."""
    _confirm_schema_and_form()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", "index")
    get_settings.cache_clear()
    assert semantic.has_schema("docs") is True
    assert semantic.has_schema("없는저장소") is False


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
    rules = f"{url}/merge-rules"
    assert c.put(rules, json={"rules": {"strip": ["상사"]}}, headers=member).status_code == 403
    assert c.put(rules, json={"rules": {"id_patterns": ["("]}}, headers=ADMIN).status_code == 409
    assert c.put(rules, json={"rules": {"strip": ["상사"]}}, headers=ADMIN).json()["strip"] == ["상사"]
    assert c.get(f"{url}/templates", headers=ADMIN).json() == {"templates": []}
    assert c.post(f"{url}/templates/propose", json={"template": "x"}, headers=member).status_code == 403
    assert c.get(f"{url}/gold", params={"kind": "pair"}, headers=member).status_code == 403
    assert c.get(f"{url}/gold", params={"kind": "bad"}, headers=ADMIN).status_code == 409
    nodes = c.get(f"{url}/gold", params={"kind": "node"}, headers=ADMIN).json()["items"]
    assert c.post(f"{url}/gold", json={"kind": "node", "item": nodes[0]["item"], "label": True},
                  headers=ADMIN).json()["node"]["labeled"] == 1
    assert c.post(f"{url}/tune", headers=ADMIN).json()["merge"]["status"] == "skipped"


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


def test_no_step_holds_the_index_locked_while_the_llm_thinks(store, monkeypatch):
    """실측: 「같은 개체 묶기: database is locked」 — 묶기가 별칭을 넣고 끝에서야 커밋해, LLM을 기다리는 몇 분 내내 쓰기
    잠금을 쥐고 있었다. 그동안 같은 색인 파일에 쓰는 것(진행 보고·주기 색인·분석)이 모두 막혔다."""
    import sqlite3
    monkeypatch.setattr(semantic, "REVIEW_EVERY", 1)       # 표본 검토도 지나가게
    real = store.answer

    def meanwhile(role, user):
        if True:    # 어느 LLM 호출이든 — 기다리는 동안 다른 쪽이 같은 파일에 쓸 수 있어야 한다
            other = sqlite3.connect(docsearch.index_path("docs"), timeout=0.2)
            try:
                other.execute("INSERT OR REPLACE INTO sem_meta (key, value) VALUES ('probe', '1')")
                other.commit()
            finally:
                other.close()
        return real(role, user)

    store.answer = meanwhile
    _confirm_schema_and_form()            # 스키마·양식 제안·검토
    with _db() as db:
        semantic.analyze(db, "docs")      # 추출·표본 검토
        result = semantic.propose_merges(db, "docs")
    assert ("A-Electronics", "에이전자(주)") in {(a["alias"], a["canonical"]) for a in result["applied"]}


# --- 비용 최적화(문서 6천 건 기준) ---

class _Recorder:
    """가짜 LLM에 들어간 system·user를 역할별로 남긴다."""
    def __init__(self, fake):
        self.fake, self.seen = fake, []

    def __call__(self, provider, messages, db=None, *a, **kw):
        self.seen.append((self.fake.role(messages[0]["content"]), messages[0]["content"], messages[1]["content"]))
        return self.fake(provider, messages, db)


def test_review_sends_only_items_and_their_evidence_not_the_document(store, monkeypatch):
    """① 검토에 문서를 다시 보내지 않는다 — 항목과 근거 문장만. 표본이 아니면 이름이 근거에 없는 항목만 검토한다."""
    rec = _Recorder(store)
    monkeypatch.setattr(semantic.llm_service, "chat_completion", rec)
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
    reviews = [user for role, _system, user in rec.seen if role == "doc_review"]
    assert len(reviews) == 1
    items = json.loads(reviews[0][reviews[0].index("항목:") + 3:])
    assert [it.get("entity", {}).get("name") for it in items] == ["전지류"]     # 이름이 근거에 없는 것만
    assert "구매팀이 배터리를 담당한다" not in reviews[0]                       # 문서 본문은 보내지 않는다
    assert "anchored" not in reviews[0]


def test_unchanged_prompt_parts_come_first_so_they_can_be_cached(store, monkeypatch):
    """④ 지시·스키마(문서마다 같다)는 system에, 문서는 user에만 — 앞부분이 같아야 프롬프트 캐시가 걸린다."""
    rec = _Recorder(store)
    monkeypatch.setattr(semantic.llm_service, "chat_completion", rec)
    semantic.confirm_schema("docs", GOOD_SCHEMA, "admin")
    with _db() as db:
        semantic.analyze(db, "docs")
    extracts = [(system, user) for role, system, user in rec.seen if role == "extract"]
    assert len(extracts) >= 2 and len({system for system, _ in extracts}) == 1      # 모든 문서에 같은 system
    assert all("협력사" in system for system, _ in extracts)                       # 스키마가 system에
    assert all(user.startswith("문서 ") and "스키마" not in user for _, user in extracts)


def test_same_body_is_analyzed_once_and_copied(store):
    """② 본문이 같은 문서(경로만 다름)는 한 번만 LLM에 보내고 결과를 옮긴다."""
    root = Path(get_settings().doc_roots.split("=")[-1] if "=" in get_settings().doc_roots else get_settings().doc_roots)
    (root / "사본").mkdir()
    (root / "사본" / "공급계약.md").write_bytes((root / "공급계약.md").read_bytes())
    docsearch.reindex("docs", root)
    semantic.confirm_schema("docs", GOOD_SCHEMA, "admin")
    with _db() as db:
        result = semantic.analyze(db, "docs")
    assert result["copied"] == 1
    assert store.calls.count("extract") == 2 + semantic.FORM_MIN_DOCS        # 사본은 부르지 않는다
    conn = semantic._conn("docs")
    try:
        a = sorted(map(tuple, conn.execute("SELECT cls, name FROM sem_nodes WHERE path = '공급계약.md'")))
        b = sorted(map(tuple, conn.execute("SELECT cls, name FROM sem_nodes WHERE path = '사본/공급계약.md'")))
    finally:
        conn.close()
    assert a and a == b
    assert semantic.status("docs")["analyzed"]["copy"] == 1


def test_work_files_and_near_empty_documents_are_not_sent(store, monkeypatch):
    """③ 업무 내용이 아닌 작업 파일(로그·설정)과 읽을 글이 거의 없는 문서는 LLM에 보내지 않는다."""
    monkeypatch.setattr(semantic, "MIN_CHARS", 30)
    root = Path(get_settings().doc_roots.split("=")[-1] if "=" in get_settings().doc_roots else get_settings().doc_roots)
    (root / "build.log").write_text("에이전자는 배터리를 공급한다. " * 20, encoding="utf-8")
    (root / "제목만.md").write_text("# 표지", encoding="utf-8")
    docsearch.reindex("docs", root)
    semantic.confirm_schema("docs", GOOD_SCHEMA, "admin")
    with _db() as db:
        result = semantic.analyze(db, "docs")
    assert result["skipped"] >= 2
    conn = semantic._conn("docs")
    try:
        reasons = {r["path"]: r["error"] for r in conn.execute("SELECT path, error FROM sem_docs WHERE status = 'skipped'")}
    finally:
        conn.close()
    assert "(.log)" in reasons["build.log"] and "읽을 글이 거의 없습니다" in reasons["제목만.md"]


def test_the_stop_rate_counts_only_the_random_sample(store, monkeypatch):
    """이름이 근거에 없는 항목은 원래 틀리기 쉽다 — 그 검토를 오류율에 섞으면 멈춤 기준을 괜히 넘는다."""
    monkeypatch.setattr(semantic, "_sampled", lambda path: False)
    monkeypatch.setattr(semantic, "STOP_MIN_ITEMS", 1)
    store.review_wrong_all = True
    real = store.answer
    store.answer = lambda role, user: ({**real(role, user), "entities": real(role, user)["entities"] + [
        {"class": "품목", "name": "전지류", "evidence": "에이전자는 배터리를 공급한다."}]}
        if role == "extract" and "에이전자는" in user else real(role, user))
    semantic.confirm_schema("docs", GOOD_SCHEMA, "admin")
    with _db() as db:
        result = semantic.analyze(db, "docs")
    status = semantic.status("docs")
    assert result["paused"] is None and status["review"]["items"] == 0     # 표본 검토는 없었다 — 멈추지 않는다
    assert status["review"]["check_items"] == 1 and status["review"]["check_wrong"] == 1
