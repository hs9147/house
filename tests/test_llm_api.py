"""LLM/모듈 API 통합 — 프로바이더 키 마스킹, 채팅→diff 제안 생성, 리뷰 엔드포인트."""
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import llm as llm_service

ADMIN = {"x-api-key": "test-admin-key"}


def _client() -> TestClient:
    return TestClient(create_app())


def _create_provider(c: TestClient, name="claude") -> int:
    r = c.post("/paas/api/v1/llm/providers", json={
        "name": name, "kind": "external", "base_url": "https://api.example.com",
        "api_key": "sk-secret", "model": "test-model",
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_project(c: TestClient, name="editor-target") -> int:
    r = c.post("/paas/api/v1/projects", json={
        "name": name, "type": "python", "git_url": "https://git.example.com/org/x",
    }, headers=ADMIN)
    return r.json()["id"]


def test_provider_key_never_exposed():
    c = _client()
    _create_provider(c)
    listing = c.get("/paas/api/v1/llm/providers", headers=ADMIN).json()
    assert listing[0]["has_api_key"] is True
    assert "sk-secret" not in str(listing)


def test_internal_provider_must_use_project_scheme():
    """보안수정 — kind=internal인데 base_url이 외부 URL이면 등록 자체를 거부한다."""
    c = _client()
    r = c.post("/paas/api/v1/llm/providers", json={
        "name": "fake-internal", "kind": "internal",
        "base_url": "https://api.some-external-llm.com", "model": "m",
    }, headers=ADMIN)
    assert r.status_code == 422
    assert "project://" in r.text


def test_internal_provider_with_project_scheme_succeeds():
    c = _client()
    r = c.post("/paas/api/v1/llm/providers", json={
        "name": "llm-main", "kind": "internal", "base_url": "project://llm-main", "model": "m",
    }, headers=ADMIN)
    assert r.status_code == 201


def _member_key(c: TestClient, name="dev1") -> dict:
    key = c.post("/paas/api/v1/keys", json={"name": name}, headers=ADMIN).json()["key"]
    return {"x-api-key": key}


def test_non_admin_key_blocked_from_external_provider_session():
    c = _client()
    pid = _create_project(c)
    prov = _create_provider(c)  # external
    r = c.post("/paas/api/v1/chat/sessions", json={"project_id": pid, "provider_id": prov},
               headers=_member_key(c))
    assert r.status_code == 403
    assert "admin" in r.text


def test_non_admin_key_allowed_for_internal_provider_session():
    c = _client()
    pid = _create_project(c)
    prov_id = c.post("/paas/api/v1/llm/providers", json={
        "name": "llm-internal", "kind": "internal", "base_url": "project://llm-internal",
        "model": "m",
    }, headers=ADMIN).json()["id"]
    r = c.post("/paas/api/v1/chat/sessions", json={"project_id": pid, "provider_id": prov_id},
               headers=_member_key(c))
    assert r.status_code == 200


def test_non_admin_key_blocked_from_external_review():
    c = _client()
    pid = _create_project(c)
    prov = _create_provider(c)  # external
    r = c.post(f"/paas/api/v1/projects/{pid}/review",
               json={"provider_id": prov, "diff": "--- a/x\n+++ b/x\n"},
               headers=_member_key(c))
    assert r.status_code == 403


def test_admin_key_still_allowed_for_external_session():
    c = _client()
    pid = _create_project(c)
    prov = _create_provider(c)
    r = c.post("/paas/api/v1/chat/sessions", json={"project_id": pid, "provider_id": prov}, headers=ADMIN)
    assert r.status_code == 200


def test_chat_message_creates_proposed_change(monkeypatch):
    reply = "수정했습니다.\n```diff\n--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x\n+y\n```"
    monkeypatch.setattr(
        llm_service, "_post_chat",
        lambda url, headers, payload: {"choices": [{"message": {"content": reply}}]},
    )
    c = _client()
    pid = _create_project(c)
    prov = _create_provider(c)

    r = c.post("/paas/api/v1/chat/sessions", json={"project_id": pid, "provider_id": prov}, headers=ADMIN)
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["branch"] == f"paas/chat-{sid}"

    r = c.post(f"/paas/api/v1/chat/sessions/{sid}/messages",
               json={"content": "m.py의 x를 y로 바꿔줘"}, headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["proposed_change_id"] is not None
    assert "```diff" in body["reply"]

    # reject 후 재적용 시도는 409
    cid = body["proposed_change_id"]
    assert c.post(f"/paas/api/v1/changes/{cid}/reject", headers=ADMIN).status_code == 204
    assert c.post(f"/paas/api/v1/changes/{cid}/apply", headers=ADMIN).status_code == 409


def test_chat_without_diff_makes_no_change(monkeypatch):
    monkeypatch.setattr(
        llm_service, "_post_chat",
        lambda url, headers, payload: {"choices": [{"message": {"content": "질문에 대한 답변만."}}]},
    )
    c = _client()
    pid = _create_project(c)
    prov = _create_provider(c)
    sid = c.post("/paas/api/v1/chat/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    body = c.post(f"/paas/api/v1/chat/sessions/{sid}/messages",
                  json={"content": "이 코드 뭐하는거야?"}, headers=ADMIN).json()
    assert body["proposed_change_id"] is None


def test_chat_context_includes_code_structure_outline(monkeypatch, tmp_path):
    """요청 2 — 채팅 시 전체 구조 개요(클래스/함수 시그니처+요약)가 LLM 컨텍스트에 주입된다."""
    import subprocess

    from app.api import llm as llm_api

    repo = tmp_path / "ws"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    # encoding 명시 필수 — Windows 기본 인코딩(cp1252)은 한글을 못 써서 UnicodeEncodeError.
    (repo / "svc.py").write_text(
        '"""결제 서비스."""\ndef charge(amount):\n    return amount\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "init"], cwd=repo, check=True)

    captured: dict = {}

    def fake_post(url, headers, payload):
        captured["messages"] = payload["messages"]
        return {"choices": [{"message": {"content": "확인했습니다."}}]}

    monkeypatch.setattr(llm_service, "_post_chat", fake_post)
    monkeypatch.setattr(llm_api.workspace, "workdir_for", lambda project: repo)

    c = _client()
    pid = _create_project(c)
    prov = _create_provider(c)
    sid = c.post("/paas/api/v1/chat/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    c.post(f"/paas/api/v1/chat/sessions/{sid}/messages",
           json={"content": "charge 함수 설명해줘"}, headers=ADMIN)

    system_text = "\n".join(m["content"] for m in captured["messages"] if m["role"] == "system")
    assert "Code structure (outline)" in system_text
    assert "svc.py" in system_text
    assert "def charge(amount)" in system_text
    assert "결제 서비스." in system_text


def test_review_endpoint_with_explicit_diff(monkeypatch):
    monkeypatch.setattr(
        llm_service, "_post_chat",
        lambda url, headers, payload: {"choices": [{"message": {"content":
            '[{"severity": "medium", "file": "a.py", "comment": "예외 처리 누락"}]'
        }}]},
    )
    c = _client()
    pid = _create_project(c)
    prov = _create_provider(c)
    r = c.post(f"/paas/api/v1/projects/{pid}/review",
               json={"provider_id": prov, "diff": "--- a/a.py\n+++ b/a.py\n"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["max_severity"] == "medium"


def test_mcp_module_bind_wires_tools_into_chat_completion(monkeypatch):
    """mcp 타입 모듈을 프로젝트에 바인딩하면 채팅 호출 시 그 서버의 도구가
    tools=로 LLM에 전달되고, 모델이 도구를 호출하면 실제 MCP 서버까지 왕복한다."""
    from app.services import mcp_client

    c = _client()
    pid = _create_project(c)
    prov = _create_provider(c)

    mid = c.post("/paas/api/v1/modules", json={
        "name": "search-mcp", "type": "mcp",
        "config": {"url": "https://mcp.example.com", "api_key": "mcp-secret"},
    }, headers=ADMIN).json()["id"]
    bind = c.post(f"/paas/api/v1/projects/{pid}/modules/{mid}/bind",
                  json={"env_prefix": "SEARCH"}, headers=ADMIN)
    assert bind.status_code == 201
    assert bind.json()["injected_env"] == ["SEARCH_API_KEY", "SEARCH_URL"]

    monkeypatch.setattr(
        mcp_client, "_post_rpc",
        lambda url, headers, payload: {"result": {"tools": [
            {"name": "web_search", "description": "웹 검색", "inputSchema": {"type": "object"}},
        ]}},
    )

    captured_payloads = []

    def fake_post_chat(url, headers, payload):
        captured_payloads.append(payload)
        if len(captured_payloads) == 1:
            return {"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "function": {
                    "name": "search-mcp__web_search", "arguments": '{"q": "chofam"}',
                }}],
            }}]}
        return {"choices": [{"message": {"content": "검색 결과를 반영했습니다."}}]}

    monkeypatch.setattr(llm_service, "_post_chat", fake_post_chat)

    def fake_post_rpc_for_call(url, headers, payload):
        if payload["method"] == "tools/list":
            return {"result": {"tools": [
                {"name": "web_search", "description": "웹 검색", "inputSchema": {"type": "object"}},
            ]}}
        assert payload["method"] == "tools/call"
        assert payload["params"] == {"name": "web_search", "arguments": {"q": "chofam"}}
        assert headers["authorization"] == "Bearer mcp-secret"
        return {"result": {"content": [{"type": "text", "text": "chofam은 사내 PaaS다"}]}}

    monkeypatch.setattr(mcp_client, "_post_rpc", fake_post_rpc_for_call)

    sid = c.post("/paas/api/v1/chat/sessions", json={"project_id": pid, "provider_id": prov},
                 headers=ADMIN).json()["id"]
    r = c.post(f"/paas/api/v1/chat/sessions/{sid}/messages",
               json={"content": "chofam이 뭐야? 검색해서 답해줘"}, headers=ADMIN)
    assert r.status_code == 200
    assert r.json()["reply"] == "검색 결과를 반영했습니다."

    assert captured_payloads[0]["tools"][0]["function"]["name"] == "search-mcp__web_search"
    second_messages = captured_payloads[1]["messages"]
    assert any(m.get("role") == "tool" and "사내 PaaS" in m.get("content", "") for m in second_messages)


def test_module_bind_and_llm_context():
    c = _client()
    pid = _create_project(c)
    r = c.post("/paas/api/v1/modules", json={
        "name": "mail", "type": "external_api",
        "config": {"url": "https://cho-fam.web.app/api/mail", "api_key": "mk-1"},
    }, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["config"]["api_key"] == "•••"
    mid = r.json()["id"]

    r = c.post(f"/paas/api/v1/projects/{pid}/modules/{mid}/bind", json={"env_prefix": "MAIL"}, headers=ADMIN)
    assert r.status_code == 201
    assert r.json()["injected_env"] == ["MAIL_API_KEY", "MAIL_URL"]

    # 같은 prefix 재사용 금지
    assert c.post(f"/paas/api/v1/projects/{pid}/modules/{mid}/bind",
                  json={"env_prefix": "MAIL"}, headers=ADMIN).status_code == 409

    ctx = c.get(f"/paas/api/v1/projects/{pid}/modules", headers=ADMIN).json()
    assert ctx == [{"name": "mail", "type": "external_api",
                    "env": ["MAIL_API_KEY", "MAIL_URL"]}]
