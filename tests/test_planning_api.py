"""에이전트 기획(Agent Planning) API — 단계 순차 진행·확정(커밋 목킹)·제약·작업 지시·MCP 서버."""
import json
import re
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN = {"x-api-key": "test-admin-key"}

_FILE_SELECT_MARK = "You select which repository files"


def _client() -> TestClient:
    return TestClient(create_app())


def _mock_llm(monkeypatch, select_reply: str = "[]", draft_reply: str = "# 초안") -> list[dict]:
    """LLM 목킹 — 파일 선정 호출과 문서 초안 호출을 시스템 프롬프트로 구분한다.

    반환된 리스트에 전송 payload가 순서대로 쌓여 컨텍스트 주입을 검증할 수 있다.
    """
    from app.services import llm as llm_service

    calls: list[dict] = []

    def fake_post(url, headers, payload):
        calls.append(payload)
        system = payload["messages"][0]["content"]
        content = select_reply if _FILE_SELECT_MARK in system else draft_reply
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(llm_service, "_post_chat", fake_post)
    return calls


def _draft_context(calls: list[dict]) -> str:
    """문서 초안 호출에 주입된 컨텍스트(두 번째 system 메시지)."""
    payload = next(p for p in calls if _FILE_SELECT_MARK not in p["messages"][0]["content"])
    return payload["messages"][1]["content"]


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
        capture_output=True,
    )


def _workspace_repo(monkeypatch, fresh_settings, tmp_path, project_name: str = "plan-app"):
    """플랫폼 워크스페이스 자리(PAAS_WORK_DIR/{project})에 실제 git 리포를 만든다."""
    from app.config import get_settings

    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    repo = tmp_path / "workspaces" / project_name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "app.py").write_text(
        '"""진입점."""\ndef main():\n    print(\'hi\')\n', encoding="utf-8")
    (repo / "README.md").write_text("# hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


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
    # 세션마다 고유한 작업 브랜치 — id 뒤에 hex 접미사가 붙는다
    assert re.fullmatch(rf"paas/plan-{body['id']}-[0-9a-f]{{8}}", body["branch"]), body["branch"]
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
    assert r.json()["document"] == "# 기획서 초안"

    # 확정 → Gitea 커밋(목킹) → 포인터 기록
    monkeypatch.setattr(workspace, "write_and_commit",
                        lambda project, branch, path, content, message: "deadbeef")
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/confirm",
               json={"content": "# 기획서 확정본"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stage"] == "spec" and body["title"] == "기획서"
    assert body["repo_path"] == "docs/agent-planning/01-기획서.md"
    assert body["commit_sha"] == "deadbeef" and body["confirmed"] is True
    # 사내 Gitea 리포가 아니면 PR은 건너뛰되 확정 자체는 성공한다
    assert body["git_action"] == "skipped"

    # 세션 조회에 확정 반영
    got = c.get(f"/paas/api/v1/plan/sessions/{sid}", headers=ADMIN).json()
    spec = next(a for a in got["artifacts"] if a["stage"] == "spec")
    assert spec["confirmed"] is True and spec["commit_sha"] == "deadbeef"

    # 이제 architecture 대화 허용
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/architecture/messages",
               json={"content": "설계"}, headers=ADMIN)
    assert r.status_code == 200


def test_stage_system_prompt_carries_agent_principles(monkeypatch):
    """기획·구현 원칙 문서(docs/agent-planning/AGENT.md)가 단계 시스템 프롬프트에 들어간다."""
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    calls = _mock_llm(monkeypatch)
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/messages",
               json={"content": "기획서 써줘"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    system = next(p for p in calls if _FILE_SELECT_MARK not in p["messages"][0]["content"]
                  )["messages"][0]["content"]
    assert "기획·구현 원칙" in system
    assert "Simplicity First" in system  # 문서 본문이 그대로 실린다
    assert system.index("Agent Planning AI") < system.index("기획·구현 원칙")  # 역할 → 원칙 → 단계 지시
    assert system.index("기획·구현 원칙") < system.index("기획서 확정")


def test_agent_principles_absent_document_injects_nothing(monkeypatch, tmp_path):
    from app.services import llm as llm_service

    monkeypatch.setattr(llm_service, "AGENT_PRINCIPLES_PATH", tmp_path / "없는파일.md")
    assert llm_service.agent_principles_prompt() == ""


def _module(c, name="orders-db", type_="database") -> int:
    return c.post("/paas/api/v1/modules", json={
        "name": name, "type": type_, "config": {"dsn": "postgres://x/y"},
    }, headers=ADMIN).json()["id"]


def test_solution_stage_binds_modules_it_decided_to_use(monkeypatch, fresh_settings, tmp_path):
    """솔루션 구성 단계에서 쓰기로 한 모듈이 그 자리에서 프로젝트에 바인딩된다."""
    from app.services import llm as llm_service, workspace

    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    _module(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    # 솔루션 단계로 가려면 앞 두 단계가 확정돼야 한다
    _mock_llm(monkeypatch)
    _confirm_spec(c, monkeypatch, sid, repo)
    monkeypatch.setattr(workspace, "write_and_commit",
                        lambda project, br, path, content, message: "cafe123")
    for stage in ("architecture",):
        assert c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/{stage}/confirm",
                      json={"content": "# 설계 확정본"}, headers=ADMIN).status_code == 200

    calls: list[dict] = []

    def fake_post(url, headers, payload):
        calls.append(payload)
        system = payload["messages"][0]["content"]
        if _FILE_SELECT_MARK in system:
            return {"choices": [{"message": {"content": "[]"}}]}
        if not any(m.get("role") == "tool" for m in payload["messages"]):
            return {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "t1", "function": {
                    "name": "bind_module",
                    "arguments": '{"module_name": "orders-db", "env_prefix": "orders"}',
                }}],
            }}]}
        return {"choices": [{"message": {"content": "# 솔루션 구성 초안"}}]}

    monkeypatch.setattr(llm_service, "_post_chat", fake_post)
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/solution/messages",
               json={"content": "솔루션 구성 써줘"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["bound_modules"] == ["orders-db"]

    # 실제로 프로젝트에 붙었고 접두사는 대문자로 정규화된다
    bound = c.get(f"/paas/api/v1/projects/{pid}/modules", headers=ADMIN).json()
    assert [b["agent_name"] for b in bound] == ["orders-db"]
    assert bound[0]["env_prefix"] == "ORDERS"

    # 도구는 솔루션 단계에서만, 가용 목록 안에서만 고를 수 있다
    tool_payload = next(p for p in calls if p.get("tools"))
    fn = tool_payload["tools"][0]["function"]
    assert fn["name"] == "bind_module"
    assert fn["parameters"]["properties"]["module_name"]["enum"] == ["orders-db"]


def test_solution_stage_offers_bound_mcp_server_tools(monkeypatch, fresh_settings, tmp_path):
    """바인딩된 MCP 서버의 도구도 솔루션 구성 단계에서 함께 쓸 수 있다."""
    from app.services import llm as llm_service, mcp_client, workspace

    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    mid = _module(c, name="docs-mcp", type_="mcp")
    # mcp 모듈은 config.url이 필요하다 — 등록 후 바인딩해 둔다(이번 턴에 이미 바인딩된 상태)
    c.put(f"/paas/api/v1/modules/{mid}",
          json={"config": {"url": "https://mcp.example.com"}}, headers=ADMIN)
    assert c.post(f"/paas/api/v1/projects/{pid}/modules/{mid}/bind",
                  json={"env_prefix": "DOCS"}, headers=ADMIN).status_code == 201

    monkeypatch.setattr(mcp_client, "list_tools", lambda url, api_key=None: [
        {"name": "search", "description": "문서 검색",
         "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    ])
    called: list[tuple] = []
    monkeypatch.setattr(mcp_client, "call_tool",
                        lambda url, key, name, args: (called.append((name, args)), "결과 3건")[1])

    _mock_llm(monkeypatch)
    _confirm_spec(c, monkeypatch, sid := c.post(
        "/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
        headers=ADMIN).json()["id"], repo)
    monkeypatch.setattr(workspace, "write_and_commit",
                        lambda project, br, path, content, message: "cafe123")
    assert c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/architecture/confirm",
                  json={"content": "# 설계 확정본"}, headers=ADMIN).status_code == 200

    payloads: list[dict] = []

    def fake_post(url, headers, payload):
        payloads.append(payload)
        if _FILE_SELECT_MARK in payload["messages"][0]["content"]:
            return {"choices": [{"message": {"content": "[]"}}]}
        if not any(m.get("role") == "tool" for m in payload["messages"]):
            return {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "t1", "function": {
                    "name": "docs-mcp__search", "arguments": '{"q": "결제 규격"}',
                }}],
            }}]}
        return {"choices": [{"message": {"content": "# 솔루션 구성 초안"}}]}

    monkeypatch.setattr(llm_service, "_post_chat", fake_post)
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/solution/messages",
               json={"content": "솔루션 구성 써줘"}, headers=ADMIN)
    assert r.status_code == 200, r.text

    # 바인딩 도구와 MCP 도구가 함께 노출되고, 호출은 해당 서버로 전달된다
    tool_payload = next(p for p in payloads if p.get("tools"))
    names = [t["function"]["name"] for t in tool_payload["tools"]]
    assert names == ["bind_module", "docs-mcp__search"]
    assert called == [("search", {"q": "결제 규격"})]
    tool_msg = next(m for m in payloads[-1]["messages"] if m.get("role") == "tool")
    assert tool_msg["content"] == "결과 3건"


def test_bind_tool_absent_outside_solution_stage(monkeypatch, fresh_settings, tmp_path):
    _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    _module(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    calls = _mock_llm(monkeypatch)
    c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/messages",
           json={"content": "기획서"}, headers=ADMIN)
    assert not any(p.get("tools") for p in calls)


def test_duplicate_bind_is_reported_not_raised(monkeypatch, fresh_settings, tmp_path):
    """도구 실패는 예외가 아니라 모델이 읽을 문장으로 돌아간다 — 문서 작성이 끊기면 안 된다."""
    from app.db import SessionLocal
    from app.models import Project as ProjectModel
    from app.services import planning as planning_service

    _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    _module(c)
    with SessionLocal() as db:
        project = db.get(ProjectModel, pid)
        bound: list[str] = []
        execute = planning_service.make_bind_executor(db, project, "tester", bound)
        assert "바인딩 완료" in execute("bind_module", {"module_name": "orders-db", "env_prefix": "ORD"})
        assert "이미 바인딩된 모듈" in execute("bind_module", {"module_name": "orders-db", "env_prefix": "X"})
        assert "찾을 수 없습니다" in execute("bind_module", {"module_name": "ghost", "env_prefix": "G"})
        assert bound == ["orders-db"]


def test_session_history_resume_and_delete(monkeypatch, fresh_settings, tmp_path):
    """세션 이력 조회 → 대화 복원(재개) → 삭제."""
    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    _mock_llm(monkeypatch, draft_reply="# 기획서 초안")
    c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/messages",
           json={"content": "요구사항 정리"}, headers=ADMIN)
    _confirm_spec(c, monkeypatch, sid, repo)

    rows = c.get("/paas/api/v1/plan/sessions", headers=ADMIN).json()
    assert [r["id"] for r in rows] == [sid]
    assert rows[0]["confirmed_stages"] == ["spec"] and rows[0]["project_name"] == "plan-app"
    assert c.get("/paas/api/v1/plan/sessions", params={"project_id": 9999},
                 headers=ADMIN).json() == []

    # 재개 — 대화가 그대로 복원된다
    messages = c.get(f"/paas/api/v1/plan/sessions/{sid}/messages", headers=ADMIN).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "요구사항 정리"

    # 삭제하면 세션의 작업 브랜치도 함께 정리된다
    deleted: list[tuple] = []
    from app.api import planning as planning_api

    monkeypatch.setattr(planning_api.workspace, "delete_branch",
                        lambda project, br: (deleted.append((project.name, br)), True)[1])
    branch = c.get(f"/paas/api/v1/plan/sessions/{sid}", headers=ADMIN).json()["branch"]
    assert c.delete(f"/paas/api/v1/plan/sessions/{sid}", headers=ADMIN).status_code == 204
    assert deleted == [("plan-app", branch)]
    assert c.get(f"/paas/api/v1/plan/sessions/{sid}", headers=ADMIN).status_code == 404
    assert c.get("/paas/api/v1/plan/sessions", headers=ADMIN).json() == []
    assert c.delete(f"/paas/api/v1/plan/sessions/{sid}", headers=ADMIN).status_code == 404


def test_reply_splits_into_chat_summary_and_artifact_body(monkeypatch, fresh_settings, tmp_path):
    """대화에는 개요만, 산출물은 편집기로 — 대화 이력에도 개요만 쌓인다."""
    _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    _mock_llm(monkeypatch, draft_reply="목적과 범위를 정리했습니다.\n---DOCUMENT---\n# 기획서\n## 1. 목적\n")
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/messages",
               json={"content": "기획서 써줘"}, headers=ADMIN)
    body = r.json()
    assert body["summary"] == "목적과 범위를 정리했습니다."
    assert body["document"] == "# 기획서\n## 1. 목적"

    messages = c.get(f"/paas/api/v1/plan/sessions/{sid}/messages", headers=ADMIN).json()
    assert messages[1]["content"] == "목적과 범위를 정리했습니다."  # 문서 본문은 이력에 쌓지 않는다

    # 마커가 없으면 전체를 문서로 보고 제목에서 개요를 만든다(산출물을 잃지 않는다)
    _mock_llm(monkeypatch, draft_reply="# 기획서\n본문\n## 2. 범위\n")
    body = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/messages",
                  json={"content": "다시"}, headers=ADMIN).json()
    assert body["document"].startswith("# 기획서")
    assert body["summary"] == "# 기획서\n## 2. 범위"


def test_generation_request_carries_current_draft_for_revision(
    monkeypatch, fresh_settings, tmp_path
):
    """생성 요청에 편집 중인 산출물을 실어 '수정'으로 이어지게 한다."""
    _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    calls = _mock_llm(monkeypatch)
    c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/messages",
           json={"content": "성공 기준 추가해줘", "draft": "# 기획서\n## 1. 목적\n결제 자동화"},
           headers=ADMIN)
    context = _draft_context(calls)
    assert "=== 현재 산출물 (수정 대상: 기획서) ===" in context
    assert "결제 자동화" in context

    # draft를 안 보내면 확정본이 대신 실린다
    calls = _mock_llm(monkeypatch)
    c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/messages",
           json={"content": "처음부터"}, headers=ADMIN)
    assert "=== 현재 산출물" not in _draft_context(calls)


def test_artifact_content_endpoint_restores_editor_on_resume(
    monkeypatch, fresh_settings, tmp_path
):
    """세션을 재개하면 확정된 산출물 본문을 편집기에 그대로 되살릴 수 있다."""
    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    _mock_llm(monkeypatch)
    _confirm_spec(c, monkeypatch, sid, repo, body="# 기획서 확정본\n요구사항 A\n")

    r = c.get(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/artifact", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "stage": "spec", "repo_path": "docs/agent-planning/01-기획서.md",
        "content": "# 기획서 확정본\n요구사항 A\n", "confirmed": True, "source": "session",
    }

    # 아직 확정 전 단계는 빈 본문
    empty = c.get(f"/paas/api/v1/plan/sessions/{sid}/stages/architecture/artifact",
                  headers=ADMIN).json()
    assert empty["content"] == "" and empty["confirmed"] is False
    assert c.get(f"/paas/api/v1/plan/sessions/{sid}/stages/nope/artifact",
                 headers=ADMIN).status_code == 404


def test_existing_repo_documents_show_up_as_artifacts(monkeypatch, fresh_settings, tmp_path):
    """리포에 이미 docs/agent-planning/*.md가 있으면 이 세션에서 확정한 적 없어도 산출물로 보인다."""
    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    doc = repo / "docs" / "agent-planning" / "01-기획서.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# 기존 기획서\n외부 도구가 남긴 문서\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs")

    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    body = c.get(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/artifact", headers=ADMIN).json()
    assert body["content"] == "# 기존 기획서\n외부 도구가 남긴 문서\n"
    assert body["source"] == "repo" and body["confirmed"] is False  # 확정은 아니다

    # 리포에 없는 단계는 그대로 빈 값
    assert c.get(f"/paas/api/v1/plan/sessions/{sid}/stages/architecture/artifact",
                 headers=ADMIN).json() == {
        "stage": "architecture", "repo_path": "docs/agent-planning/02-아키텍처설계.md",
        "content": "", "confirmed": False, "source": "",
    }

    # 생성 요청에도 수정 대상으로 실린다 — "새로 쓰기"가 아니라 "고치기"가 된다
    calls = _mock_llm(monkeypatch)
    c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/messages",
           json={"content": "성공 기준 추가"}, headers=ADMIN)
    context = _draft_context(calls)
    assert "=== 현재 산출물 (수정 대상: 기획서) ===" in context
    assert "외부 도구가 남긴 문서" in context


def test_delete_branch_never_touches_the_default_branch(monkeypatch, fresh_settings, tmp_path):
    """세션 정리가 프로젝트 기본 브랜치를 지우면 안 된다."""
    from app.db import SessionLocal
    from app.models import Project as ProjectModel
    from app.services import workspace

    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, _ = _project_and_provider(c)
    _git(repo, "checkout", "-q", "-b", "paas/plan-9-abcd1234")

    with SessionLocal() as db:
        project = db.get(ProjectModel, pid)
        assert workspace.delete_branch(project, project.branch) is False
        assert workspace.delete_branch(project, "") is False
        # 작업 브랜치는 로컬에서 실제로 사라진다(원격이 없어 push는 실패 → False)
        assert workspace.delete_branch(project, "paas/plan-9-abcd1234") is False

    out = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                         cwd=repo, capture_output=True, text=True)
    assert out.stdout.split() == ["main"]


def test_confirm_asks_before_overwriting_an_existing_repo_document(
    monkeypatch, fresh_settings, tmp_path
):
    """리포에 이미 있는 문서는 확인 없이 덮어쓰지 않는다."""
    from app.services import workspace

    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    doc = repo / "docs" / "agent-planning" / "01-기획서.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# 기존 기획서\n외부 도구가 남긴 문서\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs")

    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    committed: list[str] = []
    monkeypatch.setattr(workspace, "write_and_commit",
                        lambda project, br, path, content, message:
                        (committed.append(content), "deadbeef")[1])

    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/confirm",
               json={"content": "# 새 기획서\n"}, headers=ADMIN)
    assert r.status_code == 412
    assert "덮어쓰려면 확인이 필요합니다" in r.json()["detail"]
    assert committed == []  # 커밋되지 않았다

    # 같은 내용이면 덮어쓸 것이 없으니 묻지 않는다
    assert c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/confirm",
                  json={"content": "# 기존 기획서\n외부 도구가 남긴 문서\n"},
                  headers=ADMIN).status_code == 200

    # 확인하면 진행한다
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/confirm",
               json={"content": "# 새 기획서\n", "overwrite": True}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert committed[-1] == "# 새 기획서\n"

    # 이 세션에서 확정한 뒤에는 고칠 때마다 묻지 않는다
    assert c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/confirm",
                  json={"content": "# 또 고친 기획서\n"}, headers=ADMIN).status_code == 200


def test_session_merge_finishes_the_branch(monkeypatch, fresh_settings, tmp_path):
    """작업 지시까지 끝낸 세션은 작업 브랜치를 기본 브랜치로 반영하며 마무리된다."""
    from app.services import gitea

    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    # 확정 전에는 머지할 것이 없다
    assert c.post(f"/paas/api/v1/plan/sessions/{sid}/merge", headers=ADMIN).status_code == 409

    _mock_llm(monkeypatch)
    _confirm_spec(c, monkeypatch, sid, repo)
    monkeypatch.setattr(gitea, "repo_slug", lambda git_url: ("o", "plan-app"))
    monkeypatch.setattr(gitea, "ensure_pull_request", lambda o, r, head, base, title, body="": {
        "number": 11, "html_url": "https://git.example.com/o/plan-app/pulls/11", "mergeable": True,
    })
    monkeypatch.setattr(gitea, "merge_pull_request", lambda o, r, index, title="": True)

    body = c.post(f"/paas/api/v1/plan/sessions/{sid}/merge", headers=ADMIN).json()
    assert body["action"] == "merged"
    assert body["pull_request_url"] == "https://git.example.com/o/plan-app/pulls/11"
    assert body["branch"] == c.get(f"/paas/api/v1/plan/sessions/{sid}",
                                   headers=ADMIN).json()["branch"]

    events = c.get(f"/paas/api/v1/plan/sessions/{sid}/build-status", headers=ADMIN).json()["events"]
    assert any(e["action"] == "plan.session.merge" for e in events)


def test_every_stage_carries_default_request_prompt():
    """입력창 기본값 — 사용자가 아무것도 쓰지 않아도 바로 '초안 생성'을 누를 수 있어야 한다."""
    c = _client()
    pid, prov = _project_and_provider(c)
    body = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                  headers=ADMIN).json()
    assert all(a["default_request"].strip() for a in body["artifacts"])


def test_git_tree_is_default_context_and_prompt_selects_file_contents(
    monkeypatch, fresh_settings, tmp_path
):
    """git 파일 목록은 기본 참조, 프롬프트에 따라 선정된 파일만 본문이 붙는다."""
    _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    calls = _mock_llm(monkeypatch, select_reply='["app.py"]')
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/messages",
               json={"content": "앱 진입점 확인하고 기획서 써줘"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["context_files"] == ["app.py"]

    # 선정 호출에는 파일 목록과 사용자 요청이 함께 들어간다
    select_payload = next(p for p in calls if _FILE_SELECT_MARK in p["messages"][0]["content"])
    assert "app.py" in select_payload["messages"][1]["content"]

    context = _draft_context(calls)
    assert "=== GIT 파일 목록" in context and "README.md" in context  # 목록은 전부 기본 참조
    assert "--- app.py ---" in context and "def main():" in context  # 선정된 파일만 본문 주입
    assert "--- README.md ---" not in context
    # 코드 구조 개요도 함께 주입된다
    assert "CODE STRUCTURE (OUTLINE)" in context and "def main()" in context


def test_file_selection_failure_does_not_break_the_conversation(
    monkeypatch, fresh_settings, tmp_path
):
    _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    calls = _mock_llm(monkeypatch, select_reply="죄송합니다, 목록을 읽을 수 없습니다")
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/messages",
               json={"content": "기획서 써줘"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["context_files"] == []
    assert "=== GIT 파일 목록" in _draft_context(calls)


def test_stage_prompt_includes_previous_stage_documents(monkeypatch, fresh_settings, tmp_path):
    """각 단계는 앞 단계의 확정 문서를 프롬프트에 넣어 참조한다(세션 브랜치 커밋본 기준)."""
    from app.services import workspace

    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    branch = c.get(f"/paas/api/v1/plan/sessions/{sid}", headers=ADMIN).json()["branch"]

    _mock_llm(monkeypatch)
    monkeypatch.setattr(workspace, "write_and_commit",
                        lambda project, br, path, content, message: "deadbeef")
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/confirm",
               json={"content": "# 기획서 확정본"}, headers=ADMIN)
    assert r.status_code == 200, r.text

    # 확정 산출물은 세션 브랜치에 커밋된다. 워킹카피는 main에 남겨 두어(다른 세션이 체크아웃해 간
    # 상황) 커밋본을 읽어야만 참조되는지 확인한다.
    _git(repo, "checkout", "-q", "-b", branch)
    doc = repo / "docs" / "agent-planning" / "01-기획서.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# 기획서 확정본\n산출물 본문\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "plan(spec)")
    _git(repo, "checkout", "-q", "main")

    calls = _mock_llm(monkeypatch)
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/architecture/messages",
               json={"content": "설계 초안"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    context = _draft_context(calls)
    assert "=== 이전 단계 확정 산출물 1. 기획서" in context
    assert "산출물 본문" in context


def test_confirm_opens_pull_request_and_merges_when_mergeable(monkeypatch):
    """작업 브랜치 커밋은 PR 생성 후 머지 가능하면 자동 머지한다."""
    from app.services import gitea, workspace

    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    merged: list[tuple] = []
    monkeypatch.setattr(workspace, "write_and_commit",
                        lambda project, branch, path, content, message: "deadbeef")
    monkeypatch.setattr(gitea, "repo_slug", lambda git_url: ("o", "plan-app"))
    monkeypatch.setattr(gitea, "ensure_pull_request", lambda o, r, head, base, title, body="": {
        "number": 7, "html_url": "https://git.example.com/o/plan-app/pulls/7", "mergeable": True,
    })
    monkeypatch.setattr(gitea, "merge_pull_request",
                        lambda o, r, index, title="": (merged.append((o, r, index)), True)[1])

    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/confirm",
               json={"content": "# 기획서 확정본"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["git_action"] == "merged"
    assert body["pull_request_url"] == "https://git.example.com/o/plan-app/pulls/7"
    assert merged == [("o", "plan-app", 7)]


def test_confirm_leaves_pull_request_open_when_not_mergeable(monkeypatch):
    from app.services import gitea, workspace

    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    monkeypatch.setattr(workspace, "write_and_commit",
                        lambda project, branch, path, content, message: "deadbeef")
    monkeypatch.setattr(gitea, "repo_slug", lambda git_url: ("o", "plan-app"))
    monkeypatch.setattr(gitea, "ensure_pull_request", lambda o, r, head, base, title, body="": {
        "number": 8, "html_url": "https://git.example.com/o/plan-app/pulls/8", "mergeable": False,
    })
    monkeypatch.setattr(gitea, "merge_pull_request",
                        lambda *a, **kw: pytest.fail("머지 불가 PR을 머지하려 했다"))

    body = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/confirm",
                  json={"content": "# 확정본"}, headers=ADMIN).json()
    assert body["git_action"] == "pr_opened"
    assert "충돌" in body["git_detail"]


def test_confirm_on_default_branch_skips_pull_request(monkeypatch):
    """기본 브랜치에 직접 커밋하는 세션은 PR을 만들지 않는다."""
    from app.services import gitea, workspace

    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions",
                 json={"project_id": pid, "provider_id": prov, "branch": "main"},
                 headers=ADMIN).json()["id"]

    monkeypatch.setattr(workspace, "write_and_commit",
                        lambda project, branch, path, content, message: "deadbeef")
    monkeypatch.setattr(gitea, "ensure_pull_request",
                        lambda *a, **kw: pytest.fail("기본 브랜치인데 PR을 만들려 했다"))

    body = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/confirm",
                  json={"content": "# 확정본"}, headers=ADMIN).json()
    assert body["git_action"] == "committed"


def _confirm_spec(c, monkeypatch, sid: int, repo, body: str = "# 기획서 확정본\n요구사항 A\n"):
    """spec 단계를 확정하고, 확정본을 세션 브랜치에 실제로 커밋한다(커밋 자체는 목킹)."""
    from app.services import workspace

    monkeypatch.setattr(workspace, "write_and_commit",
                        lambda project, br, path, content, message: "deadbeef")
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/spec/confirm",
               json={"content": body}, headers=ADMIN)
    assert r.status_code == 200, r.text
    branch = c.get(f"/paas/api/v1/plan/sessions/{sid}", headers=ADMIN).json()["branch"]
    _git(repo, "checkout", "-q", "-b", branch)
    doc = repo / "docs" / "agent-planning" / "01-기획서.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "plan(spec)")
    _git(repo, "checkout", "-q", "main")


def _mcp(c, pid: int, tool: str, args: dict | None = None):
    return c.post(f"/paas/api/v1/plan/projects/{pid}/mcp", headers=ADMIN, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args or {}},
    }).json()


TASKS_JSON = ('[{"title": "결제 API 구현", "detail": "게이트웨이 경유로 호출", '
              '"verify": "pytest tests/test_pay.py 통과"},'
              ' {"title": "화면 연결", "detail": "목록 화면", "verify": "수동 확인"}]')


def test_generate_build_tasks_from_confirmed_artifacts(monkeypatch, fresh_settings, tmp_path):
    """확정 산출물에서 외주 빌더가 집어갈 작업 지시가 만들어진다."""
    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    _mock_llm(monkeypatch)
    _confirm_spec(c, monkeypatch, sid, repo)

    _mock_llm(monkeypatch, draft_reply=TASKS_JSON)
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/tasks/generate", headers=ADMIN)
    assert r.status_code == 200, r.text
    tasks = r.json()
    assert [t["title"] for t in tasks] == ["결제 API 구현", "화면 연결"]
    assert tasks[0]["verify"] == "pytest tests/test_pay.py 통과"
    assert all(t["status"] == "pending" for t in tasks)
    assert c.get(f"/paas/api/v1/plan/sessions/{sid}/tasks", headers=ADMIN).json() == tasks


def test_generate_tasks_refuses_to_overwrite_started_work(monkeypatch, fresh_settings, tmp_path):
    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    _mock_llm(monkeypatch)
    _confirm_spec(c, monkeypatch, sid, repo)
    _mock_llm(monkeypatch, draft_reply=TASKS_JSON)
    task_id = c.post(f"/paas/api/v1/plan/sessions/{sid}/tasks/generate",
                     headers=ADMIN).json()[0]["id"]

    c.patch(f"/paas/api/v1/plan/tasks/{task_id}", json={"status": "in_progress"}, headers=ADMIN)
    r = c.post(f"/paas/api/v1/plan/sessions/{sid}/tasks/generate", headers=ADMIN)
    assert r.status_code == 409

    # 확정 산출물이 없으면 애초에 만들 수 없다
    sid2 = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                  headers=ADMIN).json()["id"]
    assert c.post(f"/paas/api/v1/plan/sessions/{sid2}/tasks/generate",
                  headers=ADMIN).status_code == 409


def test_mcp_read_artifact_and_build_report_loop(monkeypatch, fresh_settings, tmp_path):
    """외부 빌더가 MCP만으로 산출물 열람 → 작업 수행 → 결과 제출 → 질의까지 한다."""
    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    _mock_llm(monkeypatch)
    _confirm_spec(c, monkeypatch, sid, repo)
    _mock_llm(monkeypatch, draft_reply=TASKS_JSON)
    tasks = c.post(f"/paas/api/v1/plan/sessions/{sid}/tasks/generate", headers=ADMIN).json()

    # clone 없이 확정 산출물 본문을 읽는다
    text = _mcp(c, pid, "read_artifact", {"stage": "spec"})["result"]["content"][0]["text"]
    assert "요구사항 A" in text
    assert "error" in _mcp(c, pid, "read_artifact", {"stage": "nope"})

    # 작업 목록 조회 → 착수 → 결과 제출
    listed = _mcp(c, pid, "list_tasks")["result"]["content"][0]["text"]
    assert "결제 API 구현" in listed
    _mcp(c, pid, "update_task", {"task_id": tasks[0]["id"], "status": "in_progress"})
    _mcp(c, pid, "submit_build_result",
         {"task_id": tasks[0]["id"], "commit_sha": "abc1234", "summary": "구현 완료"})
    done = c.get(f"/paas/api/v1/plan/sessions/{sid}/tasks", headers=ADMIN).json()[0]
    assert done["status"] == "done" and done["commit_sha"] == "abc1234"

    # 막히면 질의 — 작업은 blocked가 되고 질의는 기획 세션 대화에 남는다
    _mcp(c, pid, "request_clarification",
         {"task_id": tasks[1]["id"], "question": "결제 수단 범위가 불명확합니다"})
    blocked = c.get(f"/paas/api/v1/plan/sessions/{sid}/tasks", headers=ADMIN).json()[1]
    assert blocked["status"] == "blocked"
    events = c.get(f"/paas/api/v1/plan/sessions/{sid}/build-status", headers=ADMIN).json()["events"]
    assert any(e["action"] == "plan.build.clarification" for e in events)

    # 질의가 다음 초안 컨텍스트(대화 이력)에 실린다
    calls = _mock_llm(monkeypatch)
    c.post(f"/paas/api/v1/plan/sessions/{sid}/stages/architecture/messages",
           json={"content": "설계"}, headers=ADMIN)
    payload = next(p for p in calls if _FILE_SELECT_MARK not in p["messages"][0]["content"])
    assert any("결제 수단 범위가 불명확합니다" in m["content"] for m in payload["messages"])


VIOLATING_CODE = '''import openai

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
KEY = "sk-abcdefghijklmnopqrstuvwxyz"


def call_module():
    return get("/paas/api/v1/proxy/modules/ghost-module/query")
'''


def test_compliance_detects_llm_and_module_violations(monkeypatch, fresh_settings, tmp_path):
    """외주 결과의 LLM·모듈 사용을 검증하고, 위반 시 빌더에게 줄 수정 프롬프트를 만든다."""
    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    (repo / "agent.py").write_text(VIOLATING_CODE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "build")

    c = _client()
    pid, _ = _project_and_provider(c)
    r = c.get(f"/paas/api/v1/plan/projects/{pid}/compliance", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == {"llm_direct": 2, "hardcoded_secret": 1, "unknown_module": 1}
    assert {f["file"] for f in body["findings"]} == {"agent.py"}
    # 키 값 자체는 결과에 남기지 않는다
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(body, ensure_ascii=False)

    prompt = body["builder_prompt"]
    assert "agent.py:3" in prompt  # 파일·줄이 근거로 남는다
    assert "/paas/api/v1/proxy/llm" in prompt  # 어떻게 고칠지
    assert "가용 모듈 제약" in prompt  # 제약 원문 동봉

    # 외부 빌더는 같은 검사를 MCP로 직접 돌려 제출 전에 자기 점검한다
    text = _mcp(c, pid, "check_compliance")["result"]["content"][0]["text"]
    assert "agent.py:3" in text


def test_push_records_compliance_warning_without_blocking(monkeypatch, fresh_settings, tmp_path):
    """push는 자동으로 검사되지만 막지 않는다 — 경고만 남고 작업 목록에서 빌더가 본다."""
    import hashlib
    import hmac

    repo = _workspace_repo(monkeypatch, fresh_settings, tmp_path, project_name="plan-app")
    (repo / "agent.py").write_text(VIOLATING_CODE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "build")

    c = _client()
    pid, prov = _project_and_provider(c)
    sid = c.post("/paas/api/v1/plan/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]

    # 배포는 이 테스트의 관심사가 아니다(원격이 없으므로 실행만 막는다)
    from app.api import webhooks

    monkeypatch.setattr(webhooks, "_deploy_task", lambda project_id: None)
    body = json.dumps({
        "ref": "refs/heads/main",
        "repository": {"clone_url": "https://git.example.com/o/plan-app"},
        "commits": [{"modified": ["agent.py"], "added": [], "removed": []}],
    }).encode()
    sig = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()
    r = c.post("/paas/webhooks/git", content=body,
               headers={"x-hub-signature-256": f"sha256={sig}",
                        "content-type": "application/json"})
    assert r.json() == {"triggered": ["plan-app"]}  # 위반이 있어도 배포는 막지 않는다

    events = c.get(f"/paas/api/v1/plan/sessions/{sid}/build-status", headers=ADMIN).json()["events"]
    warning = next(e for e in events if e["action"] == "plan.build.compliance")
    assert warning["detail"]["status"] == "warning"
    assert warning["detail"]["summary"]["llm_direct"] == 2

    # 빌더는 작업 목록만 봐도 경고를 알게 된다
    text = _mcp(c, pid, "list_tasks")["result"]["content"][0]["text"]
    assert "제약 위반이 감지됐습니다" in text and "check_compliance" in text


def test_compliance_clean_repo_has_nothing_to_send(monkeypatch, fresh_settings, tmp_path):
    _workspace_repo(monkeypatch, fresh_settings, tmp_path)
    c = _client()
    pid, _ = _project_and_provider(c)
    body = c.get(f"/paas/api/v1/plan/projects/{pid}/compliance", headers=ADMIN).json()
    assert body["findings"] == [] and body["builder_prompt"] == ""
    text = _mcp(c, pid, "check_compliance")["result"]["content"][0]["text"]
    assert "위반 없음" in text


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
    # 조회(제약·모듈·산출물·작업)와 보고(진행·결과·질의·자기점검)가 모두 노출된다
    assert names == [
        "get_constraints", "list_available_modules", "report_build_progress",
        "read_artifact", "list_tasks", "update_task", "submit_build_result",
        "request_clarification", "check_compliance",
    ]

    r = c.post(base, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "report_build_progress",
                                      "arguments": {"note": "빌드 시작"}}}, headers=ADMIN)
    assert r.status_code == 200
    assert "recorded" in r.json()["result"]["content"][0]["text"]

    # 모니터링 집계에 진행 보고가 잡힌다
    status = c.get(f"/paas/api/v1/plan/sessions/{sid}/build-status", headers=ADMIN).json()
    assert any(e["action"] == "plan.build.progress" for e in status["events"])
