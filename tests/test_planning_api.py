"""에이전트 기획(Agent Planning) API — 단계 순차 진행·확정(커밋 목킹)·제약·MCP 서버."""
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
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
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
    assert "--- app.py ---\nprint('hi')" in context  # 선정된 파일만 본문 주입
    assert "--- README.md ---" not in context


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
    branch = f"paas/plan-{sid}"

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
