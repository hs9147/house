"""워크플로 — 스펙 검증, 실행(사람 단계·분기 포함), 대화 구성의 검토.

실행은 큐를 거치지만(배포와 같은 패턴) 여기서는 `_execute`를 직접 부른다 — 스레드가 끝나길
기다리는 테스트는 느리고, 느려서 못 믿게 되면 아무도 안 돌린다. 큐 경계 자체는 창구 테스트가
`_submit`을 인라인으로 바꿔 확인한다.
"""
import json

import pytest

from app.db import SessionLocal
from app.main import create_app  # 표를 만든다(Base.metadata.create_all) — 다른 테스트와 같다
from app.models import (
    LlmProvider, LlmProviderKind, Organization, Workflow, WorkflowRun, WorkflowRunStatus,
)
from app.services import workflow as wf
from app.services import workflowchat


@pytest.fixture(autouse=True)
def _tables(monkeypatch):
    # conftest의 _clean_db가 테스트마다 test-paas.db를 지운다 — 표는 테스트마다 만든다.
    create_app()
    # 큐는 끈다. resume()이 큐에 넣고 우리가 _execute를 또 부르면 **같은 실행이 두 번**
    # 돌아 서로의 커밋을 덮는다(처음엔 테스트가 운으로 통과했다).
    monkeypatch.setattr(wf, "_submit", lambda run_id: None)


@pytest.fixture
def org():
    with SessionLocal() as db:
        row = Organization(name=f"wf-org-{id(db)}")
        db.add(row)
        db.commit()
        yield row.id
        db.query(Workflow).filter(Workflow.organization_id == row.id).delete()
        db.delete(row)
        db.commit()


@pytest.fixture
def docs_store(monkeypatch, tmp_path, fresh_settings):
    """읽기 가능한 저장소 하나 — 워크플로가 실제로 만지는 자원이다."""
    from app.config import get_settings

    root = tmp_path / "docs"
    root.mkdir()
    (root / "계약서.md").write_text("# 용역계약\n금액 1억 2천만원\n", encoding="utf-8")
    monkeypatch.setenv("PAAS_DOC_ROOTS", f"docs={root}")
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    get_settings.cache_clear()
    return root


def _make(db, org_id: int, spec: dict, name="w") -> Workflow:
    row = Workflow(organization_id=org_id, name=name, spec=spec)
    db.add(row)
    db.commit()
    return row


# --- 검증: 저장을 막는 일 ---

def test_empty_spec_is_rejected(org, docs_store):
    with SessionLocal() as db:
        assert wf.validate(db, org, {"nodes": [], "edges": []})[0].startswith("nodes가 비어")


def test_unknown_type_lists_what_is_available(org, docs_store):
    with SessionLocal() as db:
        problems = wf.validate(db, org, {
            "nodes": [{"id": "가", "type": "email.send", "to": "a@b"}], "edges": [],
        })
    assert any("모르는 종류" in p and "storage.list" in p for p in problems), problems


def test_unknown_field_is_not_silently_ignored(org, docs_store):
    """적었는데 안 듣는 설정이 가장 나쁘다 — 쓰이지 않는 항목은 문제로 말한다."""
    with SessionLocal() as db:
        problems = wf.validate(db, org, {
            "nodes": [{"id": "목록", "type": "storage.list", "store": "docs", "매직": 1}],
            "edges": [],
        })
    assert any("쓰이지 않는 항목 '매직'" in p for p in problems), problems


def test_missing_store_and_readonly_write_are_caught(org, docs_store):
    with SessionLocal() as db:
        problems = wf.validate(db, org, {
            "nodes": [
                {"id": "목록", "type": "storage.list", "store": "없는저장소"},
                {"id": "쓰기", "type": "storage.write", "store": "docs", "path": "out.md"},
            ],
            "edges": [{"from": "목록", "to": "쓰기"}],
        })
    assert any("'없는저장소'가 없습니다" in p for p in problems), problems


def test_cycle_and_orphan_are_caught(org, docs_store):
    with SessionLocal() as db:
        problems = wf.validate(db, org, {
            "nodes": [
                {"id": "가", "type": "storage.list", "store": "docs"},
                {"id": "나", "type": "doc.read"},
                {"id": "외톨이", "type": "storage.list", "store": "docs"},
            ],
            "edges": [{"from": "가", "to": "나"}, {"from": "나", "to": "가"}],
        })
    assert any("고리를 이룹니다" in p for p in problems), problems
    assert any("외톨이: 아무 단계와도" in p for p in problems), problems


def test_branch_needs_a_true_or_false_edge(org, docs_store):
    with SessionLocal() as db:
        problems = wf.validate(db, org, {
            "nodes": [
                {"id": "목록", "type": "storage.list", "store": "docs"},
                {"id": "판정", "type": "branch", "when": {"kind": "contains", "text": "1억"}},
            ],
            "edges": [{"from": "목록", "to": "판정"}],
        })
    assert any("참/거짓 연결이 없습니다" in p for p in problems), problems


def test_case_only_on_branch_edges(org, docs_store):
    with SessionLocal() as db:
        problems = wf.validate(db, org, {
            "nodes": [
                {"id": "목록", "type": "storage.list", "store": "docs"},
                {"id": "읽기", "type": "doc.read"},
            ],
            "edges": [{"from": "목록", "to": "읽기", "case": "참"}],
        })
    assert any("분기 단계에서 나가는 연결에만" in p for p in problems), problems


def test_a_valid_spec_has_no_problems(org, docs_store):
    with SessionLocal() as db:
        assert wf.validate(db, org, {
            "nodes": [
                {"id": "목록", "type": "storage.list", "store": "docs", "suffix": ".md"},
                {"id": "읽기", "type": "doc.read", "store": "docs"},
                {"id": "검토", "type": "human", "title": "법무 검토", "role": "법무"},
            ],
            "edges": [{"from": "목록", "to": "읽기"}, {"from": "읽기", "to": "검토"}],
        }) == []


# --- 실행 ---

def test_run_reads_files_and_stops_at_the_human_step(org, docs_store):
    """사람 단계에서 멈추고 **앞 단계 출력을 남긴다** — 몇 시간 뒤에 이어지므로."""
    with SessionLocal() as db:
        row = _make(db, org, {
            "nodes": [
                {"id": "목록", "type": "storage.list", "store": "docs", "suffix": ".md"},
                {"id": "읽기", "type": "doc.read", "store": "docs"},
                {"id": "검토", "type": "human", "title": "법무 검토"},
            ],
            "edges": [{"from": "목록", "to": "읽기"}, {"from": "읽기", "to": "검토"}],
        })
        run = WorkflowRun(workflow_id=row.id, version=row.version,
                          status=WorkflowRunStatus.running, steps=[], outputs={})
        db.add(run)
        db.commit()
        wf._execute(db, run)

        assert run.status == WorkflowRunStatus.waiting
        assert run.pending_node == "검토"
        assert run.outputs["목록"]["paths"] == ["계약서.md"]
        assert "금액 1억 2천만원" in run.outputs["읽기"]["text"]

        # 제출하면 그 자리에서 이어 돈다(남은 단계가 없으므로 성공으로 끝난다).
        wf.resume(db, run, "승인합니다", approved=True)
        wf._execute(db, run)
        assert run.status == WorkflowRunStatus.succeeded
        assert run.outputs["검토"]["text"] == "승인합니다"
        assert [s["status"] for s in run.steps] == ["ok", "ok", "waiting", "ok"]


def test_rejecting_a_human_step_ends_the_run_with_the_reason(org, docs_store):
    with SessionLocal() as db:
        row = _make(db, org, {
            "nodes": [{"id": "검토", "type": "human", "title": "법무 검토"}], "edges": [],
        })
        run = WorkflowRun(workflow_id=row.id, version=1,
                          status=WorkflowRunStatus.running, steps=[], outputs={})
        db.add(run)
        db.commit()
        wf._execute(db, run)
        assert run.status == WorkflowRunStatus.waiting

        wf.resume(db, run, "금액 근거가 없습니다", approved=False)
        assert run.status == WorkflowRunStatus.canceled
        assert "금액 근거가 없습니다" in run.error
        assert run.steps[-1]["status"] == "rejected"


def test_branch_runs_one_side_and_skips_the_other(org, docs_store, monkeypatch):
    with SessionLocal() as db:
        row = _make(db, org, {
            "nodes": [
                {"id": "목록", "type": "storage.list", "store": "docs", "suffix": ".md"},
                {"id": "읽기", "type": "doc.read", "store": "docs"},
                {"id": "판정", "type": "branch", "when": {"kind": "contains", "text": "1억"}},
                {"id": "승인", "type": "human", "title": "임원 승인"},
                {"id": "자동", "type": "storage.write", "store": "docs", "path": "자동.md"},
            ],
            "edges": [
                {"from": "목록", "to": "읽기"}, {"from": "읽기", "to": "판정"},
                {"from": "판정", "to": "승인", "case": "참"},
                {"from": "판정", "to": "자동", "case": "거짓"},
            ],
        })
        run = WorkflowRun(workflow_id=row.id, version=1,
                          status=WorkflowRunStatus.running, steps=[], outputs={})
        db.add(run)
        db.commit()
        wf._execute(db, run)

        assert run.outputs["판정"]["case"] == "참"      # 본문에 "1억"이 있다
        assert run.status == WorkflowRunStatus.waiting   # 참 쪽의 사람 단계에서 멈춘다
        assert run.pending_node == "승인"

        # 사람 단계는 **흐름 전체**를 멈춘다(그 결정을 기다리는 것이 업무의 모양이다).
        # 거짓 쪽이 건너뛰어졌다는 판정은 이어 돌 때 기록된다.
        wf.resume(db, run, "승인", approved=True)
        wf._execute(db, run)
        steps = {s["id"]: s["status"] for s in run.steps}
        assert steps["자동"] == "skipped"
        assert run.status == WorkflowRunStatus.succeeded
        assert not (docs_store / "자동.md").exists()     # 거짓 쪽은 파일을 쓰지 않았다


def test_llm_step_uses_the_default_provider_and_the_previous_output(org, docs_store, monkeypatch):
    seen: dict = {}

    def fake_chat(provider, messages, db=None, **kw):
        seen["provider"] = provider.name
        seen["user"] = messages[-1]["content"]
        return "검토 의견: 금액이 기준을 넘습니다."

    monkeypatch.setattr(wf.llm, "chat_completion", fake_chat)
    with SessionLocal() as db:
        provider = LlmProvider(name=f"p-{id(db)}", kind=LlmProviderKind.external,
                               base_url="https://x/v1", model="m", is_default=True)
        db.add(provider)
        db.commit()
        try:
            row = _make(db, org, {
                "nodes": [
                    {"id": "읽기", "type": "doc.read", "store": "docs", "path": "계약서.md"},
                    {"id": "검토", "type": "llm", "prompt": "계약 금액을 확인하라"},
                ],
                "edges": [{"from": "읽기", "to": "검토"}],
            })
            run = WorkflowRun(workflow_id=row.id, version=1,
                              status=WorkflowRunStatus.running, steps=[], outputs={})
            db.add(run)
            db.commit()
            wf._execute(db, run)
            assert run.status == WorkflowRunStatus.succeeded
            assert "검토 의견" in run.outputs["검토"]["text"]
            assert seen["provider"] == provider.name
            # 앞 단계 본문이 프롬프트에 실린다 — 안 실리면 LLM은 아무것도 모른다.
            assert "금액 1억 2천만원" in seen["user"]
        finally:
            db.delete(provider)
            db.commit()


def test_a_failed_step_fails_the_run_with_the_reason(org, docs_store):
    with SessionLocal() as db:
        row = _make(db, org, {
            "nodes": [
                {"id": "목록", "type": "storage.list", "store": "docs", "suffix": ".없음"},
                {"id": "읽기", "type": "doc.read", "store": "docs"},
            ],
            "edges": [{"from": "목록", "to": "읽기"}],
        })
        run = WorkflowRun(workflow_id=row.id, version=1,
                          status=WorkflowRunStatus.running, steps=[], outputs={})
        db.add(run)
        db.commit()
        wf._execute(db, run)
        assert run.status == WorkflowRunStatus.failed
        assert "읽을 경로가 없습니다" in run.error


# --- 대화 구성과 검토 ---

def test_parse_survives_code_fences_and_prose():
    data = workflowchat._parse(
        '설명입니다\n```json\n{"summary": "s", "spec": {"nodes": []}}\n```\n끝')
    assert data["summary"] == "s" and data["spec"] == {"nodes": []}


def test_review_flags_constraints_that_no_step_enforces():
    notes = workflowchat.review(
        {"nodes": [{"id": "검토", "type": "human", "title": "법무"}]},
        {"entities": [{"name": "계약서"}],
         "states": [{"entity": "계약서", "name": "검토대기"}],
         "transitions": [{"from": "검토대기", "to": "승인", "trigger": "법무 승인",
                          "node": "검토"}],
         "constraints": [{"text": "1억 초과는 임원 승인", "origin": "대화", "node": ""}]},
        ["선급금 30% 초과는 법무팀 합의"],
    )
    assert any("지키는 단계가 없습니다" in n for n in notes), notes
    # 등록해 둔 업무 제약사항을 아예 읽지 않았으면 그것도 검토 대상이다.
    assert any("업무 제약사항이 읽히지 않았습니다" in n for n in notes), notes


def test_review_flags_transitions_pointing_at_missing_steps():
    notes = workflowchat.review(
        {"nodes": [{"id": "검토", "type": "human", "title": "법무"}]},
        {"transitions": [{"from": "a", "to": "b", "node": "없는단계"}]},
        [],
    )
    assert any("없는 단계 '없는단계'" in n for n in notes), notes


def test_propose_repairs_once_when_validation_rejects(org, docs_store, monkeypatch):
    """LLM이 처음에 틀린 스펙을 주면 **문제 목록을 돌려주고 한 번 고치게 한다.**"""
    calls: list[list[dict]] = []

    def fake_chat(provider, messages, db=None, **kw):
        calls.append(messages)
        if len(calls) == 1:
            return '{"summary": "1차", "spec": {"nodes": [{"id": "가", "type": "모름"}],' \
                   ' "edges": []}}'
        return ('{"summary": "고쳤다", "spec": {"nodes": [{"id": "목록",'
                ' "type": "storage.list", "store": "docs"}], "edges": []},'
                ' "extracted": {"entities": [{"name": "계약서"}]}}')

    monkeypatch.setattr(workflowchat.llm, "chat_completion", fake_chat)
    with SessionLocal() as db:
        provider = LlmProvider(name=f"p2-{id(db)}", kind=LlmProviderKind.external,
                               base_url="https://x/v1", model="m", is_default=True)
        db.add(provider)
        db.commit()
        try:
            row = _make(db, org, {"nodes": [], "edges": []}, name="w2")
            result = workflowchat.propose(db, row, "계약서를 모아 검토한다", [])
        finally:
            db.delete(provider)
            db.commit()
    assert result["attempts"] == 2
    assert result["problems"] == []
    assert result["spec"]["nodes"][0]["id"] == "목록"
    # 2차 요청에는 1차의 문제 목록이 실린다 — 그게 수선의 근거다.
    assert "모르는 종류" in calls[1][-1]["content"]
    # 업무 제약사항과 자원 목록은 시스템 메시지에 실린다(하네싱).
    assert "반드시 지켜야 하는 이 조직의 업무 제약사항" in calls[0][0]["content"]
    assert "storage.list" in calls[0][0]["content"]


def test_review_accepts_a_paraphrased_common_constraint():
    """모델은 문장을 풀어 쓴다 — 앞부분 문자열로 맞춰 보면 그대로 실은 규칙도 '빠뜨렸다'가
    된다(실측에서 그랬다). 낱말 겹침으로 본다."""
    notes = workflowchat.review(
        {"nodes": [{"id": "체결", "type": "human", "title": "계약 체결"}]},
        {"constraints": [{
            "text": "해외 협력사 또는 비표준계약서는 오프라인 날인 신청을 진행한다.",
            "origin": "공통", "node": "체결"}]},
        ["해외 협력사/비표준계약서는 오프라인 날인 신청(체결진행품의)"],
    )
    assert notes == [], notes


def test_review_still_flags_a_constraint_that_was_not_read():
    notes = workflowchat.review(
        {"nodes": [{"id": "체결", "type": "human", "title": "계약 체결"}]},
        {"constraints": [{"text": "계약은 전자서명으로 체결한다", "node": "체결"}]},
        ["선급금 30퍼센트 초과는 법무팀 합의가 필요하다"],
    )
    assert any("읽히지 않았습니다" in n for n in notes), notes


# --- 평가: 사람 단계를 에이전트로 옮길 수 있는가 ---

def _assess_spec(db, org_id):
    return _make(db, org_id, {
        "nodes": [
            {"id": "수집", "type": "human", "title": "신용평가서 수집"},
            {"id": "검토", "type": "human", "title": "지표 검토"},
            {"id": "승인", "type": "human", "title": "실장 승인", "role": "실장"},
        ],
        "edges": [{"from": "수집", "to": "검토"}, {"from": "검토", "to": "승인"}],
    }, name="assess")


def test_assessment_counts_are_recomputed_not_trusted(org, docs_store, monkeypatch):
    """판정은 모델이 하고 **셈은 우리가 한다.** 모델이 적은 숫자와 목록이 어긋나는 경우가
    있고, 그때 화면에 믿을 수 없는 비율이 뜨면 평가 자체를 아무도 안 본다."""
    from app.services import workflowassess

    def fake_chat(provider, messages, db=None, **kw):
        return json.dumps({
            "summary": "요약",
            "steps": [
                {"id": "수집", "verdict": "agent", "why": "파일만 모으면 된다",
                 "becomes": ["storage.list", "doc.read"], "change": "자동 수집으로 바꾼다",
                 "needs": []},
                {"id": "검토", "verdict": "partial", "why": "초안은 자동, 판단은 사람",
                 "becomes": ["llm"], "change": "LLM 초안을 앞에 둔다",
                 "needs": ["신용평가 저장소 바인딩"]},
                {"id": "승인", "verdict": "human", "why": "결재 권한", "becomes": [],
                 "change": "", "needs": []},
            ],
            "missing": ["GPS 발주 시스템 MCP 모듈"],
            "risks": ["오판 시 계약 지연"],
        }, ensure_ascii=False)

    monkeypatch.setattr(workflowassess.llm, "chat_completion", fake_chat)
    with SessionLocal() as db:
        provider = LlmProvider(name=f"p3-{id(db)}", kind=LlmProviderKind.external,
                               base_url="https://x/v1", model="m", is_default=True)
        db.add(provider)
        db.commit()
        try:
            out = workflowassess.assess(db, _assess_spec(db, org))
        finally:
            db.delete(provider)
            db.commit()
    m = out["metrics"]
    # 사람 단계 수와 '사람 유지' 판정 수는 **다른 값**이다(한 키에 담으면 하나가 덮인다).
    assert m["human"] == 3 and m["human_only"] == 1
    assert (m["agent"], m["partial"], m["assessed"]) == (1, 1, 3)
    assert m["shift_rate"] == round(2 / 3 * 100, 1)
    # 자원이 더 필요한 항목은 '지금 가능'이 아니다.
    assert m["ready_now"] == 1
    assert [s["ready_now"] for s in out["steps"]] == [True, False, False]
    assert out["notes"] == []


def test_assessment_drops_made_up_steps_and_tools_and_says_so(org, docs_store, monkeypatch):
    from app.services import workflowassess

    def fake_chat(provider, messages, db=None, **kw):
        return json.dumps({
            "summary": "",
            "steps": [
                {"id": "없는단계", "verdict": "agent", "becomes": []},
                {"id": "수집", "verdict": "agent", "becomes": ["email.send", "doc.read"]},
            ],
        }, ensure_ascii=False)

    monkeypatch.setattr(workflowassess.llm, "chat_completion", fake_chat)
    with SessionLocal() as db:
        provider = LlmProvider(name=f"p4-{id(db)}", kind=LlmProviderKind.external,
                               base_url="https://x/v1", model="m", is_default=True)
        db.add(provider)
        db.commit()
        try:
            out = workflowassess.assess(db, _assess_spec(db, org))
        finally:
            db.delete(provider)
            db.commit()
    assert [s["id"] for s in out["steps"]] == ["수집"]
    assert out["steps"][0]["becomes"] == ["doc.read"]          # 없는 도구는 버린다
    assert any("스펙에 없는 단계" in n for n in out["notes"])
    assert any("쓸 수 없는 노드 종류" in n for n in out["notes"])
    # 평가되지 않은 사람 단계가 있으면 "평가했다"가 거짓이 된다 — 드러낸다.
    assert any("평가되지 않은 사람 단계: 검토, 승인" in n for n in out["notes"])


def test_change_request_carries_every_shiftable_step_with_its_prerequisites():
    """처음에는 전제(needs)가 없는 것만 실었다 — 사내 업무에서는 거의 모든 단계에 전제가
    붙어서(실측 24/24) 요청문이 비고 버튼이 보이지 않았다. 깨끗하지만 아무것도 못 바꾼다."""
    from app.services import workflowassess

    text = workflowassess.change_request({"steps": [
        {"id": "검토", "verdict": "partial", "ready_now": False, "change": "LLM 초안을 앞에",
         "becomes": ["llm"], "needs": ["신용평가 저장소"]},
        {"id": "수집", "verdict": "agent", "ready_now": True, "change": "자동 수집으로",
         "becomes": ["storage.list"], "needs": []},
        {"id": "승인", "verdict": "human", "ready_now": False, "change": "", "becomes": []},
    ]})
    lines = text.splitlines()
    assert lines[1].startswith("- 수집:")        # 전제 없는 것이 먼저
    assert "검토" in text and "전제: 신용평가 저장소" in text
    assert "승인" not in text                     # 사람으로 남길 단계는 싣지 않는다
    # 옮길 단계가 하나도 없으면 빈 문자열 — 화면이 버튼을 감춘다.
    assert workflowassess.change_request(
        {"steps": [{"id": "승인", "verdict": "human"}]}) == ""


def test_decorated_node_types_are_accepted_not_flagged():
    """실측: 프롬프트가 `- storage.list(파일 목록)`이라 모델이 그 꾸밈말까지 적어 보냈고,
    운영 평가 한 번에 오탐 7건이 떴다. 거짓이 섞인 검토 목록은 아무도 읽지 않는다."""
    from app.services import workflowassess

    allowed = set(wf.NODE_TYPES)
    assert workflowassess._node_type("storage.list(파일 목록)", allowed) == "storage.list"
    assert workflowassess._node_type(" doc.read ", allowed) == "doc.read"
    assert workflowassess._node_type("파일 목록", allowed) == "storage.list"   # 라벨만 적은 경우
    assert workflowassess._node_type("email.send", allowed) == ""            # 없는 것은 없다


# --- 온톨로지와 .ready를 실제로 쓰는가 ---

def test_graph_find_reads_the_ontology_and_hands_paths_to_doc_read(org, monkeypatch,
                                                                   tmp_path, fresh_settings):
    """색인이 뽑아 둔 그래프를 워크플로가 **쓴다.**

    전까지는 노드 종류 8개 중 어느 것도 그래프를 보지 않았다 — 문서에서 뽑은 노드 28,301개가
    워크플로에는 없는 것과 같았다. 경로를 함께 내는 것이 요점이다: 출력 규약이 하나라서
    다음 단계의 doc.read가 "그 표가 있는 문서를 전부 읽어"가 된다.
    """
    from app.config import get_settings
    from app.services import docsearch

    root = tmp_path / "docs"
    root.mkdir()
    (root / "견적서.md").write_text(
        "# 견적 요청\n\n| 구분 | 금액 |\n|---|---|\n| 자재 | 100 |\n", encoding="utf-8")
    (root / "메모.md").write_text("# 메모\n표도 용어도 없다.\n", encoding="utf-8")
    monkeypatch.setenv("PAAS_DOC_ROOTS", f"docs={root}")
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("PAAS_DOC_READY_DIR", str(tmp_path / "ready"))
    get_settings.cache_clear()
    docsearch.reindex("docs", root)   # 색인이 돌면 그래프와 .ready가 함께 만들어진다

    with SessionLocal() as db:
        row = _make(db, org, {
            "nodes": [
                {"id": "표찾기", "type": "graph.find", "store": "docs", "kind": "table"},
                {"id": "읽기", "type": "doc.read", "store": "docs"},
            ],
            "edges": [{"from": "표찾기", "to": "읽기"}],
        }, name="graph")
        assert wf.validate(db, org, row.spec) == []
        run = WorkflowRun(workflow_id=row.id, version=1,
                          status=WorkflowRunStatus.running, steps=[], outputs={})
        db.add(run)
        db.commit()
        wf._execute(db, run)

        assert run.status == WorkflowRunStatus.succeeded, run.error
        # 표 노드가 있는 문서만 찾아야 한다(메모.md에는 표가 없다).
        assert run.outputs["표찾기"]["paths"] == ["견적서.md"]
        assert "구분 | 금액" in run.outputs["표찾기"]["text"]
        # 경로가 다음 단계로 흘러 **본문**(.ready 마크다운)이 읽힌다.
        assert "| 구분 | 금액 |" in run.outputs["읽기"]["text"]


def test_unknown_graph_kind_is_rejected_not_silently_empty(org, docs_store):
    """없는 kind를 적으면 0건이 나온다 — 0건과 "그런 종류가 없다"는 다른 사실이다."""
    with SessionLocal() as db:
        problems = wf.validate(db, org, {
            "nodes": [{"id": "찾기", "type": "graph.find", "store": "docs", "kind": "표"}],
            "edges": [],
        })
    assert any("모르는 노드 종류 '표'" in p for p in problems), problems
