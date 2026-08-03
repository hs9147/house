"""에이전트 기획(Agent Planning) API — 단계 순차 진행·확정(커밋 목킹)·제약·MCP 서버."""
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN = {"x-api-key": "test-admin-key"}


def _client() -> TestClient:
    return TestClient(create_app())


def _project_and_provider(c: TestClient) -> tuple[int, int]:
    pid = c.post("/paas/api/v1/projects", json={
        "name": "plan-app", "type": "python", "git_url": "https://git.example.com/o/plan-app",
    }, headers=ADMIN).json()["id"]
    prov = c.post("/paas/api/v1/llm/providers", json={
        "name": "p", "kind": "openai", "base_url": "https://api.example.com",
        "api_key": "sk-secret", "model": "m",
    }, headers=ADMIN).json()["id"]
    return pid, prov


def test_session_lists_all_four_stages_unconfirmed():
    c = _client()
    pid, prov = _project_and_provider(c)
    r = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov}, headers=ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["branch"].startswith("paas/plan-")
    stages = [a["stage"] for a in body["artifacts"]]
    assert stages == ["spec", "architecture", "solution", "principles"]
    assert all(a["confirmed"] is False for a in body["artifacts"])


def test_stage_order_enforced_and_confirm_commits(monkeypatch):
    from app.services import llm as llm_service
    from app.services import workspace

    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    # 앞 단계(spec) 미확정 상태에서 architecture 대화는 409
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/architecture/messages",
               json={"content": "설계 초안"}, headers=ADMIN)
    assert r.status_code == 409

    # spec 단계 대화 → 문서 초안 반환(LLM 목킹)
    monkeypatch.setattr(llm_service, "_post_chat",
                        lambda url, headers, payload: {"choices": [{"message": {"content": "# 기획서 초안"}}]})
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/messages",
               json={"content": "요구사항 정리해줘"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "# 기획서 초안"

    # 확정 → Gitea 커밋(목킹) → 포인터 기록
    monkeypatch.setattr(workspace, "write_and_commit",
                        lambda project, branch, path, content, message: "deadbeef")
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/confirm",
               json={"content": "# 기획서 확정본"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "stage": "spec", "title": "기획서",
        "repo_path": "docs/agent-planning/01-기획서.md",
        "commit_sha": "deadbeef", "confirmed": True,
    }

    # 세션 조회에 확정 반영
    got = c.get(f"/paas/api/v1/plan/sessions/{sid}", headers=ADMIN).json()
    spec = next(a for a in got["artifacts"] if a["stage"] == "spec")
    assert spec["confirmed"] is True and spec["commit_sha"] == "deadbeef"

    # 이제 architecture 대화 허용
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/architecture/messages",
               json={"content": "설계"}, headers=ADMIN)
    assert r.status_code == 200


def test_constraints_document_lists_rules():
    c = _client()
    pid, _ = _project_and_provider(c)
    r = c.get(f"/paas/api/v1/plan/projects/{pid}/constraints", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"] == "plan-app"
    assert any("게이트웨이" in rule for rule in body["rules"])
    assert "가용 모듈 제약" in body["document"]


def test_mcp_server_tools_and_progress_report():
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    base = f"/paas/api/v1/plan/projects/{pid}/mcp"

    r = c.post(base, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=ADMIN)
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert names == ["get_constraints", "list_available_modules", "report_build_progress"]

    r = c.post(base, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "report_build_progress",
                                      "arguments": {"note": "빌드 시작"}}}, headers=ADMIN)
    assert r.status_code == 200
    assert "recorded" in r.json()["result"]["content"][0]["text"]

    # 모니터링 집계에 진행 보고가 잡힌다
    status = c.get(f"/paas/api/v1/plan/sessions/{sid}/build-status", headers=ADMIN).json()
    assert any(e["action"] == "plan.build.progress" for e in status["events"])
