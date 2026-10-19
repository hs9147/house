"""워크플로 ↔ 저장소 온톨로지 연결(services/workflowlinks).

문서 온톨로지는 문서에서만 나오고(재색인 없이) 그 위에 워크플로 쪽 선을 긋는다:
  A  설계(단계 → 저장소) · 실행 기록(단계 → 실제로 읽은 문서)
  B  이름 일치(대상·상태 → 용어·조문·표) · 전문 색인(많이 나오는 문서) · 규칙 속 제N조·「문서」
  C  LLM 제안 — 후보 목록 안에서만, 사람이 확인해야 쓰인다
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import docsearch, gitea, llm
from app.services import workflow as wf

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"

RULE = "「구매규정」 제12조에 따라 1억 이상은 실장이 승인한다"


@pytest.fixture
def client(monkeypatch, tmp_path, fresh_settings):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "구매규정.md").write_text(
        "# 구매규정\n\n제12조(승인) 1억 이상은 실장이 승인한다.\n\n"
        '제2조(정의) "협력사"란 물품을 공급하는 업체를 말한다.\n\n'
        "| 협력사명 | 단가 |\n|---|---|\n| 가나 | 100 |\n", encoding="utf-8")
    (root / "평가메모.md").write_text("협력사 협력사 협력사 협력사 평가 메모\n", encoding="utf-8")
    (root / "보안.md").write_text("# 보안 규칙\n\n외부 반출 금지\n", encoding="utf-8")
    monkeypatch.setenv("PAAS_DOC_ROOTS", f"docs={root}")
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "")
    monkeypatch.setattr(gitea, "ensure_org", lambda name: None)
    get_settings.cache_clear()

    def inline(run_id: int) -> None:
        from app.db import SessionLocal
        from app.models import WorkflowRun

        with SessionLocal() as session:
            run = session.get(WorkflowRun, run_id)
            if run is not None:
                wf._execute(session, run)

    monkeypatch.setattr(wf, "_submit", inline)
    c = TestClient(create_app())
    docsearch.reindex("docs", root)
    c.root = root
    return c


def _flow(c: TestClient) -> int:
    org = c.post(f"{API}/orgs", json={"name": "gp"}, headers=ADMIN).json()["id"]
    from app.db import SessionLocal
    from app.models import DocumentStore
    # 환경변수 저장소의 DB 이관은 DB마다 한 번이라, 다른 시험이 쓴 DB에서는 행이 없을 수 있다.
    with SessionLocal() as db:
        row = db.query(DocumentStore).filter(DocumentStore.name == "docs").one_or_none()
        if row is None:
            row = DocumentStore(name="docs", root_path=str(c.root), read_only=False, active=True)
            db.add(row)
        row.root_path, row.organization_id = str(c.root), org
        db.commit()
    wid = c.post(f"{API}/workflows", headers=ADMIN, json={"organization_id": org, "name": "구매"}).json()["id"]
    saved = c.put(f"{API}/workflows/{wid}", headers=ADMIN, json={
        "spec": {"nodes": [{"id": "찾기", "type": "docs.search", "store": "docs", "query": "협력사"}], "edges": []},
        "extracted": {"entities": [{"name": "협력사", "note": ""}],
                      "states": [{"entity": "협력사", "name": "승인", "note": ""}],
                      "transitions": [],
                      "constraints": [{"text": RULE, "origin": "대화", "node": ""},
                                      {"text": "보안 규칙을 지킨다", "origin": "대화", "node": ""}]}})
    assert saved.status_code == 200, saved.text
    return wid


def _rows(out, **match):
    return [link for link in out["links"] if all(link[k] == v for k, v in match.items())]


def test_design_names_and_index_are_linked_when_the_workflow_is_saved(client):
    wid = _flow(client)
    out = client.get(f"{API}/workflows/{wid}/links", headers=ADMIN).json()
    assert out["stores"] == ["docs"]
    # A — 설계: 단계가 쓰는 저장소
    assert _rows(out, subject="찾기", rel="uses", target="docs", source="spec")
    # B — 이름: 같은 이름의 용어(정확히)와 그 낱말이 든 표 컬럼, 조문 제목
    entity = {(r["target_kind"], r["target"]) for r in _rows(out, subject="협력사", rel="matches")}
    assert ("term", "협력사") in entity and ("table", "협력사명 | 단가") in entity
    assert ("section", "제12조(승인)") in {(r["target_kind"], r["target"]) for r in _rows(out, subject="승인")}
    # B — 전문 색인: 많이 나오는 문서가 먼저
    mentions = _rows(out, subject="협력사", rel="mentions")
    assert mentions[0]["path"] == "평가메모.md" and mentions[0]["detail"].startswith("4번")
    # B — 규칙 속 제N조·「문서」 → 근거
    grounds = {(r["target_kind"], r["target"]) for r in _rows(out, subject=RULE, rel="grounded_in")}
    assert ("section", "제12조(승인)") in grounds and ("document", "구매규정") in grounds
    assert out["unground_rules"] == ["보안 규칙을 지킨다"]
    # 그림 — 워크플로가 가운데, 낱말·단계를 지나 저장소의 노드로
    kinds = {n["kind"] for n in out["graph"]["nodes"]}
    assert {"workflow", "entity", "state", "constraint", "step", "term", "section", "document"} <= kinds


def test_a_finished_run_adds_what_the_step_actually_read(client):
    wid = _flow(client)
    run = client.post(f"{API}/workflows/{wid}/runs", headers=ADMIN)
    assert run.status_code == 201, run.text
    out = client.get(f"{API}/workflows/{wid}/links", headers=ADMIN).json()
    read = {r["path"] for r in _rows(out, subject="찾기", rel="read", source="run")}
    assert {"구매규정.md", "평가메모.md"} <= read


def test_a_run_paused_at_a_human_step_already_counts_what_it_read(client):
    """사람 단계에서 멈춘 실행(waiting)도 — 앞 단계가 읽은 문서는 이미 사실이다."""
    wid = _flow(client)
    client.put(f"{API}/workflows/{wid}", headers=ADMIN, json={"spec": {
        "nodes": [{"id": "찾기", "type": "docs.search", "store": "docs", "query": "협력사"},
                  {"id": "검토", "type": "human", "title": "팀장 검토"}],
        "edges": [{"from": "찾기", "to": "검토"}]}})
    run = client.post(f"{API}/workflows/{wid}/runs", headers=ADMIN).json()
    assert client.get(f"{API}/workflows/runs/{run['id']}", headers=ADMIN).json()["status"] == "waiting"
    out = client.get(f"{API}/workflows/{wid}/links", headers=ADMIN).json()
    assert _rows(out, subject="찾기", rel="read", source="run")


def test_llm_suggestions_come_only_from_candidates_and_need_confirmation(client, monkeypatch):
    wid = _flow(client)
    assert client.post(f"{API}/workflows/{wid}/links/suggest", headers=ADMIN).status_code == 400   # LLM 없음

    seen = {}

    def fake(provider, messages, db=None, **kw):
        seen["prompt"] = messages[0]["content"]
        return json.dumps({"links": [{"subject": 0, "candidate": 0, "reason": "같은 규칙"},
                                     {"subject": 0, "candidate": 999, "reason": "지어낸 것"}]})

    monkeypatch.setattr(llm, "default_provider", lambda db: object())
    monkeypatch.setattr(llm, "chat_completion", fake)
    out = client.post(f"{API}/workflows/{wid}/links/suggest", headers=ADMIN).json()
    assert out["suggested"] == 1                              # 후보 밖 번호는 버린다
    assert "보안 규칙을 지킨다" in seen["prompt"] and "보안 규칙" in seen["prompt"]
    proposal = _rows(out, source="llm")[0]
    assert proposal["confirmed"] is False and proposal["subject"] == "보안 규칙을 지킨다"
    assert "보안 규칙을 지킨다" in out["unground_rules"]       # 확인 전에는 근거가 아니다

    # 다시 계산해도 제안은 남는다
    client.post(f"{API}/workflows/{wid}/links/refresh", headers=ADMIN)
    assert client.patch(f"{API}/workflows/{wid}/links/{proposal['id']}", headers=ADMIN,
                        json={"confirmed": True}).json()["confirmed"] is True
    assert client.get(f"{API}/workflows/{wid}/links", headers=ADMIN).json()["unground_rules"] == []
    # 거절 = 지우기
    assert client.delete(f"{API}/workflows/{wid}/links/{proposal['id']}", headers=ADMIN).status_code == 204


def test_smartwork_task_carries_the_workflow_stores_and_grounds(client):
    wid = _flow(client)
    from app.db import SessionLocal
    from app.models import Workflow
    from app.services import workflowlinks

    with SessionLocal() as db:
        stores, grounds = workflowlinks.prompt_lines(db, db.get(Workflow, wid))
    assert stores == ["docs"]
    assert any("협력사 → 같은 이름: term 협력사" in g for g in grounds)
    assert any("근거: section 제12조(승인) (docs/구매규정.md)" in g for g in grounds)


def test_links_go_with_the_workflow_and_other_orgs_cannot_see_them(client):
    wid = _flow(client)
    key = client.post(f"{API}/keys", json={"name": "outsider"}, headers=ADMIN).json()["key"]
    assert client.get(f"{API}/workflows/{wid}/links", headers={"x-api-key": key}).status_code == 404
    assert client.delete(f"{API}/workflows/{wid}", headers=ADMIN).status_code == 204
    from app.db import SessionLocal
    from app.models import WorkflowLink
    with SessionLocal() as db:
        assert db.query(WorkflowLink).filter(WorkflowLink.workflow_id == wid).count() == 0
