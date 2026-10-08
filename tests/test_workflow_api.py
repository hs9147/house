"""워크플로 창구 — 조직 단위 생성, 저장 거부, 구성 대화, 실행과 사람 단계.

실행은 작업 큐를 거친다. 여기서는 큐를 **인라인**으로 바꿔 경계까지 확인한다 — 스레드가
끝나길 기다리는 테스트는 느리고 가끔 흔들린다.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import workflow as wf
from app.services import workflowchat
from app.services import gitea

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


@pytest.fixture
def client(monkeypatch, tmp_path, fresh_settings):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "계약서.md").write_text("# 용역계약\n금액 1억\n", encoding="utf-8")
    monkeypatch.setenv("PAAS_DOC_ROOTS", f"docs={root}")
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setattr(gitea, "ensure_org", lambda name: None)
    get_settings.cache_clear()

    # 큐를 인라인으로 — 요청이 돌아왔을 때 실행이 이미 끝나 있어야 단정을 쓸 수 있다.
    def inline(run_id: int) -> None:
        from app.db import SessionLocal
        from app.models import WorkflowRun

        with SessionLocal() as session:
            run = session.get(WorkflowRun, run_id)
            if run is not None:
                wf._execute(session, run)

    monkeypatch.setattr(wf, "_submit", inline)
    return TestClient(create_app())


def _org(c: TestClient, name="gp") -> int:
    result = c.post(f"{API}/orgs", json={"name": name}, headers=ADMIN)
    assert result.status_code == 201, result.text
    org_id = result.json()["id"]
    from app.db import SessionLocal
    from app.models import DocumentStore
    with SessionLocal() as db:
        docs = db.query(DocumentStore).filter(DocumentStore.name == "docs").one_or_none()
        if docs is not None and docs.organization_id is None:
            docs.organization_id = org_id
            db.commit()
    return org_id


def _member(c: TestClient) -> dict:
    key = c.post(f"{API}/keys", json={"name": "dev1"}, headers=ADMIN).json()["key"]
    return {"x-api-key": key}


def test_workflows_are_created_per_organization(client):
    org1, org2 = _org(client, "gp"), _org(client, "vs")
    first = client.post(f"{API}/workflows", headers=ADMIN,
                        json={"organization_id": org1, "name": "계약검토"})
    assert first.status_code == 201, first.text
    assert first.json()["org_name"] == "gp"

    # 한 조직에 여러 개를 둘 수 있고, 같은 이름은 **다른 조직에서는** 쓸 수 있다.
    assert client.post(f"{API}/workflows", headers=ADMIN,
                       json={"organization_id": org1, "name": "신규업체등록"}
                       ).status_code == 201
    assert client.post(f"{API}/workflows", headers=ADMIN,
                       json={"organization_id": org2, "name": "계약검토"}).status_code == 201
    # 같은 조직 안에서는 이름이 겹칠 수 없다.
    assert client.post(f"{API}/workflows", headers=ADMIN,
                       json={"organization_id": org1, "name": "계약검토"}).status_code == 409

    mine = client.get(f"{API}/workflows", params={"organization_id": org1},
                      headers=ADMIN).json()
    assert sorted(w["name"] for w in mine) == ["계약검토", "신규업체등록"]
    assert client.post(f"{API}/workflows", headers=ADMIN,
                       json={"organization_id": 9999, "name": "x"}).status_code == 404


def test_saving_an_invalid_spec_is_rejected_with_every_problem(client):
    org = _org(client)
    wid = client.post(f"{API}/workflows", headers=ADMIN,
                      json={"organization_id": org, "name": "w"}).json()["id"]
    bad = client.put(f"{API}/workflows/{wid}", headers=ADMIN, json={"spec": {
        "nodes": [{"id": "가", "type": "email.send"},
                  {"id": "나", "type": "storage.list", "store": "없음"}],
        "edges": [{"from": "가", "to": "나"}],
    }})
    assert bad.status_code == 400, bad.text
    problems = bad.json()["detail"]["problems"]
    assert any("모르는 종류" in p for p in problems)
    assert any("없습니다" in p for p in problems)

    good = client.put(f"{API}/workflows/{wid}", headers=ADMIN, json={"spec": {
        "nodes": [{"id": "목록", "type": "storage.list", "store": "docs"},
                  {"id": "검토", "type": "human", "title": "법무 검토"}],
        "edges": [{"from": "목록", "to": "검토"}],
    }, "extracted": {"entities": [{"name": "계약서"}]}})
    assert good.status_code == 200, good.text
    assert good.json()["version"] == 2          # 저장마다 판이 올라간다
    assert good.json()["summary"]["human_steps"] == 1
    assert good.json()["extracted"]["entities"][0]["name"] == "계약서"


def test_members_can_read_but_not_change(client):
    org = _org(client)
    wid = client.post(f"{API}/workflows", headers=ADMIN,
                      json={"organization_id": org, "name": "w"}).json()["id"]
    member = _member(client)
    assert client.get(f"{API}/workflows/{wid}", headers=member).status_code == 200
    assert client.put(f"{API}/workflows/{wid}", headers=member,
                      json={"spec": {"nodes": [], "edges": []}}).status_code == 403
    assert client.post(f"{API}/workflows/{wid}/runs", headers=member).status_code == 403
    assert client.delete(f"{API}/workflows/{wid}", headers=member).status_code == 403
    assert client.get(f"{API}/workflows/{wid}").status_code == 401


def test_resources_are_the_same_list_the_llm_sees(client):
    org = _org(client)
    body = client.get(f"{API}/workflows/resources", params={"organization_id": org},
                      headers=ADMIN).json()
    assert [s["name"] for s in body["stores"]] == ["docs"]
    assert {t["type"] for t in body["node_types"]} == set(wf.NODE_TYPES)
    assert body["cases"] == ["참", "거짓"]


def test_chat_proposes_a_spec_and_keeps_the_conversation(client, monkeypatch):
    org = _org(client)
    wid = client.post(f"{API}/workflows", headers=ADMIN,
                      json={"organization_id": org, "name": "계약검토"}).json()["id"]
    # 이 워크플로가 **이미 읽어 둔** 제약을 심어 둔다 — 다시 쓸 때 그것이 실리는지 본다.
    # 제약은 조직이 아니라 워크플로가 갖는다(흐름마다 규칙이 다르다).
    client.put(f"{API}/workflows/{wid}", headers=ADMIN, json={
        "spec": {"nodes": [{"id": "검토", "type": "human", "title": "법무 검토"}],
                 "edges": []},
        "extracted": {"constraints": [
            {"text": "선급금 30% 초과는 법무팀 합의가 필요하다", "node": "검토"}]},
    })
    # 기획의 공통 제약사항(에이전트 개발 제한)은 여기 실리지 않는다(아래에서 확인).
    client.post(f"{API}/plan/constraints", headers=ADMIN,
                json={"text": "서버는 80포트만 쓰고 IIS에서 URL rewrite한다"})
    created = client.post(f"{API}/llm/providers", headers=ADMIN, json={
        "name": "p1", "kind": "openai", "base_url": "https://x/v1",
        "model": "m", "api_key": "k",
    })
    assert created.status_code in (200, 201), created.text
    provider = created.json()
    assert client.post(f"{API}/llm/providers/{provider['id']}/default",
                       headers=ADMIN).status_code == 200

    prompts: list[str] = []

    def fake_chat(provider, messages, db=None, **kw):
        prompts.append(messages[0]["content"])
        return ('{"summary": "계약서를 모아 법무 검토를 붙였습니다.",'
                ' "spec": {"nodes": [{"id": "목록", "type": "storage.list", "store": "docs"},'
                ' {"id": "검토", "type": "human", "title": "법무 검토"}],'
                ' "edges": [{"from": "목록", "to": "검토"}]},'
                ' "extracted": {"entities": [{"name": "계약서", "note": "검토 대상"}],'
                ' "states": [{"entity": "계약서", "name": "검토대기"}],'
                ' "transitions": [{"from": "검토대기", "to": "승인", "trigger": "법무 검토",'
                ' "node": "검토"}],'
                ' "constraints": [{"text": "선급금 30% 초과는 법무팀 합의가 필요하다",'
                ' "origin": "공통", "node": "검토"}]}}')

    monkeypatch.setattr(workflowchat.llm, "chat_completion", fake_chat)
    answer = client.post(f"{API}/workflows/{wid}/chat", headers=ADMIN,
                         json={"request": "계약서를 모아 법무가 검토하게 해 주세요"})
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["problems"] == []
    assert body["attempts"] == 1
    assert body["extracted"]["entities"][0]["name"] == "계약서"
    assert body["review"] == []            # 제약이 단계에 걸려 있으면 검토할 것이 없다
    # 이 워크플로의 제약이 프롬프트에 실린다(하네싱).
    assert "선급금 30% 초과" in prompts[0]
    # 기획의 개발 제약은 실리지 않는다 — 섞으면 "이 규칙을 지키는 단계가 없습니다"가
    # 엉뚱한 데서 뜬다(실측에서 그랬다).
    assert "80포트" not in prompts[0]

    # 제안은 저장되지 않는다 — 사람이 저장을 눌러야 한다(앞서 저장한 한 단계가 그대로다).
    saved = client.get(f"{API}/workflows/{wid}", headers=ADMIN).json()
    assert [n["id"] for n in saved["spec"]["nodes"]] == ["검토"]
    assert saved["constraints"] == ["선급금 30% 초과는 법무팀 합의가 필요하다"]
    history = client.get(f"{API}/workflows/{wid}/messages", headers=ADMIN).json()
    assert [m["role"] for m in history] == ["user", "assistant"]


def test_run_stops_at_the_human_step_and_resumes_on_submit(client):
    org = _org(client)
    wid = client.post(f"{API}/workflows", headers=ADMIN,
                      json={"organization_id": org, "name": "계약검토"}).json()["id"]
    client.put(f"{API}/workflows/{wid}", headers=ADMIN, json={"spec": {
        "nodes": [{"id": "목록", "type": "storage.list", "store": "docs", "suffix": ".md"},
                  {"id": "검토", "type": "human", "title": "법무 검토", "role": "법무"}],
        "edges": [{"from": "목록", "to": "검토"}],
    }})
    run = client.post(f"{API}/workflows/{wid}/runs", headers=ADMIN)
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]

    detail = client.get(f"{API}/workflows/runs/{run_id}", headers=ADMIN).json()
    assert detail["status"] == "waiting" and detail["pending_node"] == "검토"
    assert detail["outputs"]["목록"]["paths"] == ["계약서.md"]

    done = client.post(f"{API}/workflows/runs/{run_id}/submit", headers=ADMIN,
                       json={"content": "승인합니다", "approved": True})
    assert done.status_code == 200, done.text
    assert client.get(f"{API}/workflows/runs/{run_id}", headers=ADMIN).json()["status"] \
        == "succeeded"
    # 끝난 실행에는 다시 제출할 수 없다.
    assert client.post(f"{API}/workflows/runs/{run_id}/submit", headers=ADMIN,
                       json={"content": "또", "approved": True}).status_code == 409


def test_running_an_invalid_spec_is_refused(client):
    org = _org(client)
    wid = client.post(f"{API}/workflows", headers=ADMIN,
                      json={"organization_id": org, "name": "w"}).json()["id"]
    # 빈 스펙 그대로 — 저장을 거치지 않았으므로 실행 요청에서 막아야 한다.
    refused = client.post(f"{API}/workflows/{wid}/runs", headers=ADMIN)
    assert refused.status_code == 400
    assert "nodes가 비어" in refused.json()["detail"]


def test_provider_failure_is_reported_not_swallowed_as_500(client, monkeypatch):
    """프로바이더 쪽 사유는 그대로 올린다.

    실측: Bedrock SSO 토큰이 만료됐을 때 화면에 "Internal Server Error"만 떴다 — 메시지에는
    어느 프로필로 재로그인하면 되는지까지 적혀 있었는데 그게 로그에만 남았다.
    """
    from app.services import bedrock

    org = _org(client)
    wid = client.post(f"{API}/workflows", headers=ADMIN,
                      json={"organization_id": org, "name": "w"}).json()["id"]
    created = client.post(f"{API}/llm/providers", headers=ADMIN, json={
        "name": "p1", "kind": "openai", "base_url": "https://x/v1", "model": "m",
        "api_key": "k",
    }).json()
    client.post(f"{API}/llm/providers/{created['id']}/default", headers=ADMIN)

    def boom(*a, **kw):
        raise bedrock.BedrockError("AWS SSO 토큰이 만료됐습니다 (프로필 'p') — 재로그인하세요.")

    monkeypatch.setattr(workflowchat.llm, "chat_completion", boom)
    answer = client.post(f"{API}/workflows/{wid}/chat", headers=ADMIN,
                         json={"request": "만들어 주세요"})
    assert answer.status_code == 502, answer.status_code
    assert "재로그인" in answer.json()["detail"]


def test_assessment_endpoint_reports_and_offers_a_change_request(client, monkeypatch):
    """평가는 읽고 끝나지 않는다 — 그대로 다음 구성 요청이 되는 문장을 함께 준다."""
    from app.services import workflowassess

    org = _org(client)
    wid = client.post(f"{API}/workflows", headers=ADMIN,
                      json={"organization_id": org, "name": "계약검토"}).json()["id"]
    client.put(f"{API}/workflows/{wid}", headers=ADMIN, json={"spec": {
        "nodes": [{"id": "수집", "type": "human", "title": "계약서 수집"},
                  {"id": "승인", "type": "human", "title": "실장 승인"}],
        "edges": [{"from": "수집", "to": "승인"}],
    }})
    created = client.post(f"{API}/llm/providers", headers=ADMIN, json={
        "name": "p1", "kind": "openai", "base_url": "https://x/v1", "model": "m",
        "api_key": "k",
    }).json()
    client.post(f"{API}/llm/providers/{created['id']}/default", headers=ADMIN)

    monkeypatch.setattr(workflowassess.llm, "chat_completion", lambda *a, **kw: (
        '{"summary": "수집은 자동화할 수 있습니다.", "steps": ['
        '{"id": "수집", "verdict": "agent", "why": "파일 수집", '
        '"becomes": ["storage.list"], "change": "저장소 목록으로 바꾼다", "needs": []},'
        '{"id": "승인", "verdict": "human", "why": "결재 권한", "becomes": [], '
        '"change": "", "needs": []}], "missing": [], "risks": []}'))

    out = client.post(f"{API}/workflows/{wid}/assessment", headers=ADMIN)
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["metrics"]["human"] == 2 and body["metrics"]["human_only"] == 1
    assert body["metrics"]["agent"] == 1 and body["metrics"]["ready_now"] == 1
    assert "수집: 저장소 목록으로 바꾼다" in body["change_request"]

    # 단계가 없으면 평가할 것도 없다(400).
    empty = client.post(f"{API}/workflows", headers=ADMIN,
                        json={"organization_id": org, "name": "빈것"}).json()["id"]
    assert client.post(f"{API}/workflows/{empty}/assessment", headers=ADMIN).status_code == 400
    # 비관리자는 평가를 돌릴 수 없다(LLM 호출이고 감사 로그에 남는다).
    assert client.post(f"{API}/workflows/{wid}/assessment",
                       headers=_member(client)).status_code == 403


def test_rename_does_not_bump_the_version_or_revalidate(client):
    """이름을 고친 것은 새 판이 아니고, 스펙에 문제가 있어도 이름은 고칠 수 있어야 한다."""
    org = _org(client)
    wid = client.post(f"{API}/workflows", headers=ADMIN,
                      json={"organization_id": org, "name": "구매요청"}).json()["id"]
    client.put(f"{API}/workflows/{wid}", headers=ADMIN, json={"spec": {
        "nodes": [{"id": "목록", "type": "storage.list", "store": "docs"},
                  {"id": "검토", "type": "human", "title": "법무 검토"}],
        "edges": [{"from": "목록", "to": "검토"}],
    }})
    assert client.get(f"{API}/workflows/{wid}", headers=ADMIN).json()["version"] == 2

    renamed = client.patch(f"{API}/workflows/{wid}", headers=ADMIN,
                           json={"name": "구매요청-사전검토"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "구매요청-사전검토"
    assert renamed.json()["version"] == 2          # 판은 그대로다
    assert len(renamed.json()["spec"]["nodes"]) == 2

    # 같은 조직의 다른 워크플로 이름으로는 바꿀 수 없다.
    client.post(f"{API}/workflows", headers=ADMIN,
                json={"organization_id": org, "name": "신규업체등록"})
    assert client.patch(f"{API}/workflows/{wid}", headers=ADMIN,
                        json={"name": "신규업체등록"}).status_code == 409
    # 비어 있는 이름은 받지 않고, 비관리자는 바꿀 수 없다.
    assert client.patch(f"{API}/workflows/{wid}", headers=ADMIN,
                        json={"name": ""}).status_code == 422
    assert client.patch(f"{API}/workflows/{wid}", headers=_member(client),
                        json={"name": "x"}).status_code == 403
