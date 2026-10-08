"""스마트워크 — 개인 업무 맥락(동의·폴더 동기화·메일·철회)과 대화(도구·화면 선택).

LLM은 chat_completion을 대본으로 바꿔 끼워 **도구 호출을 실제로 실행**시킨다 — 개인
도구가 남의 저장소를 못 보는지는 도구가 돌아야 확인된다. 메일은 브라우저가 Graph에서
읽어 보내므로 서버 쪽 시험에는 Graph가 없다.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import (
    Deployment, DeploymentStatus, PersonalContext, Project, Workflow, WorkflowRun,
    WorkflowRunStatus,
)
from app.services import docsearch, gitea, llm, personal, smartwork

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


@pytest.fixture
def client(monkeypatch, tmp_path, fresh_settings):
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setenv("PAAS_DOC_ROOTS", f"docs={docs}")
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_PERSONAL_ROOT", str(tmp_path / "personal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("PAAS_ALLOWED_EMAIL_DOMAIN", "")
    # 조직 생성은 사내 Gitea에 조직을 만든다 — 테스트가 실제 Gitea 설정에 기대지 않게 한다.
    monkeypatch.setattr(gitea, "ensure_org", lambda name: None)
    get_settings.cache_clear()
    c = TestClient(create_app())
    r = c.post(f"{API}/llm/providers", json={
        "name": "claude", "kind": "openai", "base_url": "https://api.example.com",
        "api_key": "sk-secret", "model": "test-model",
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    return c


def _user(c: TestClient, email: str, org_id: int | None = None) -> dict:
    c.post(f"{API}/auth/register", json={"email": email, "name": "user", "password": "pw12345"})
    account_id = next(a["id"] for a in c.get(f"{API}/auth/accounts", headers=ADMIN).json()
                      if a["email"] == email)
    c.post(f"{API}/auth/accounts/{account_id}/approve", headers=ADMIN)
    if org_id is not None:
        c.post(f"{API}/auth/accounts/{account_id}/organizations/modify",
               json={"organization_id": org_id, "action": "add"}, headers=ADMIN)
    key = c.post(f"{API}/auth/login", json={"email": email, "password": "pw12345"}).json()["key"]
    return {"x-api-key": key}


def _upload(c, headers, folder, files: dict[str, bytes], mtime=1_700_000_000.0):
    entries = [{"path": p, "size": len(b), "mtime": mtime} for p, b in files.items()]
    plan = c.post(f"{API}/smartwork/personal/folders/{folder}/manifest",
                  json={"entries": entries}, headers=headers)
    assert plan.status_code == 200, plan.text
    needed = plan.json()["needed"]
    if needed:
        r = c.post(
            f"{API}/smartwork/personal/folders/{folder}/files",
            files=[("files", (Path(p).name, files[p])) for p in needed],
            data={"paths": needed, "mtimes": [str(mtime)] * len(needed)},
            headers=headers,
        )
        assert r.status_code == 200, r.text
    return plan.json()


class Script:
    """chat_completion 대역 — 주어진 도구 호출을 차례로 실행하고 결과를 모아 둔다."""

    def __init__(self, calls: list[tuple[str, dict]], reply: str = "완료"):
        self.calls, self.reply = calls, reply
        self.results: dict[str, str] = {}
        self.tool_names: list[str] = []
        self.messages: list[dict] = []

    def __call__(self, provider, messages, db=None, tools=None, tool_executor=None):
        self.messages = messages
        self.tool_names = [t["function"]["name"] for t in tools or []]
        for name, args in self.calls:
            self.results[name] = tool_executor(name, args)
        return self.reply


def _chat(c, headers, monkeypatch, calls, text="질문", messages=None):
    script = Script(calls)
    monkeypatch.setattr(llm, "chat_completion", script)
    body = {"messages": messages if messages is not None else [{"role": "user", "content": text}]}
    r = c.post(f"{API}/smartwork/chat", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json(), script


def test_personal_context_requires_consent(client):
    alice = _user(client, "alice@corp.com")
    status = client.get(f"{API}/smartwork/personal", headers=alice).json()
    assert status["consented"] is False
    r = client.post(f"{API}/smartwork/personal/folders/work/manifest",
                    json={"entries": []}, headers=alice)
    assert r.status_code == 400 and "동의" in r.json()["detail"]


def test_folder_sync_indexes_only_documents_and_tracks_changes(client):
    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    files = {"plan/q3.md": "# 3분기 계획\n\n오로라 프로젝트 일정".encode(), "photo.png": b"\x89PNG"}
    plan = _upload(client, alice, "work", files)
    assert plan["needed"] == ["plan/q3.md"]  # 사진은 문서가 아니다

    store = personal.store_for("alice@corp.com")
    assert docsearch.search(store.name, "오로라")["hits"][0]["path"] == "work/plan/q3.md.md"
    # 원본은 남지 않는다 — 저장소에는 변환본만 있다
    assert sorted(p.relative_to(store.root).as_posix() for p in store.root.rglob("*") if p.is_file())         == ["work/plan/q3.md.md"]

    # 다시 골라도 같은 파일은 다시 올리지 않는다
    assert _upload(client, alice, "work", files)["needed"] == []
    # PC에서 지운 파일은 여기서도 지운다
    gone = _upload(client, alice, "work", {})
    assert gone["removed"] == 1
    assert docsearch.search(store.name, "오로라")["hits"] == []
    status = client.get(f"{API}/smartwork/personal", headers=alice).json()
    assert status["folders"][0]["name"] == "work"


def test_unconvertible_file_is_recorded_not_kept(client):
    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    broken = {"scan.pdf": b"%PDF-1.4 broken"}
    assert _upload(client, alice, "work", broken)["needed"] == ["scan.pdf"]

    store = personal.store_for("alice@corp.com")
    assert not any(p.is_file() for p in store.root.rglob("*"))  # 원본도 변환본도 없다
    # 원본이 그대로면 다시 받지 않고, 실패로 센다
    assert _upload(client, alice, "work", broken)["needed"] == []
    index = client.get(f"{API}/smartwork/personal", headers=alice).json()["index"]
    assert index["total"] == 1 and index["failed"] == 1
    # PC에서 지우면 실패 기록도 빠진다
    assert _upload(client, alice, "work", {})["removed"] == 1
    assert client.get(f"{API}/smartwork/personal", headers=alice).json()["index"]["failed"] == 0


def test_personal_context_is_visible_only_to_its_owner(client, monkeypatch):
    alice = _user(client, "alice@corp.com")
    bob = _user(client, "bob@corp.com")
    for who in (alice, bob):
        client.post(f"{API}/smartwork/personal/consent", headers=who)
    _upload(client, alice, "work", {"memo.md": "# 메모\n\n비밀코드 zebra-42".encode()})

    out, script = _chat(client, alice, monkeypatch, [("my__search", {"query": "zebra-42"})])
    assert "work/memo.md" in script.results["my__search"]

    # bob의 개인 도구는 bob의 저장소에 묶여 있다 — 고를 인자가 없다
    _, script = _chat(client, bob, monkeypatch, [("my__search", {"query": "zebra-42"}),
                                                 ("my__read", {"path": "work/memo.md"})])
    assert json.loads(script.results["my__search"])["hits"] == []
    assert "파일이 없습니다" in script.results["my__read"]
    # 경로로 빠져나가지도 못한다
    _, script = _chat(client, bob, monkeypatch, [("my__read", {"path": "../"})])
    assert "도구 오류" in script.results["my__read"]

    # 사내 문서 검색(/mcp/docs)에는 개인 저장소가 없다
    r = client.post(f"{API}/mcp/docs", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_sources", "arguments": {}},
    }, headers=ADMIN)
    text = r.json()["result"]["content"][0]["text"]
    assert "_me-" not in text and "zebra" not in text


def test_revoke_deletes_files_index_and_row(client):
    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    _upload(client, alice, "work", {"memo.md": b"# memo\n\nhello"})
    store = personal.store_for("alice@corp.com")
    assert store.root.is_dir() and docsearch.index_path(store.name).exists()

    assert client.delete(f"{API}/smartwork/personal", headers=alice).status_code == 204
    assert not store.root.exists()
    assert not docsearch.index_path(store.name).exists()
    assert client.get(f"{API}/smartwork/personal", headers=alice).json()["consented"] is False


def _running_app(name: str, org_id: int | None = None) -> None:
    with SessionLocal() as db:
        project = Project(name=name, type="react", git_url=f"https://git.test/{name}",
                          organization_id=org_id)
        db.add(project)
        db.flush()
        db.add(Deployment(project_id=project.id, git_sha="a" * 40, image_tag="",
                          profile="release", status=DeploymentStatus.running))
        db.commit()


def test_chat_shows_agent_and_report(client, monkeypatch):
    _running_app("leave-agent")
    alice = _user(client, "alice@corp.com")
    out, script = _chat(client, alice, monkeypatch, [
        ("show_agent", {"name": "leave-agent", "reason": "휴가 신청"}),
        ("show_report", {"title": "요약", "format": "md", "content": "# 요약"}),
    ], text="휴가 신청하고 싶어")
    assert out["agent"]["name"] == "leave-agent"
    assert out["agent"]["path"] == "/apps/_/leave-agent/"
    assert out["report"] == {"title": "요약", "format": "md", "content": "# 요약"}
    assert out["tools"] == ["show_agent", "show_report"]

    names = set(script.tool_names)
    assert {"show_agent", "docs__search_docs", "graph__find_nodes", "storage__list_files",
            "apis__search_apis", "code__read_file"} <= names
    # 쓰기·갱신 도구와 운영 도구(관리자 전용)는 없다. 동의하지 않았으니 개인 도구도 없다.
    assert not names & {"docs__reindex_docs", "apis__sync_catalog"}
    assert not any(n.startswith(("ops__", "my__")) for n in names)
    # 감사에는 도구 이름만 남는다
    events = [e for e in client.get(f"{API}/audit", headers=ADMIN).json()
              if e["action"] == "smartwork.chat"]
    assert events[0]["detail"] == {"tools": ["show_agent", "show_report"]}

    _, script = _chat(client, ADMIN, monkeypatch, [])
    assert "ops__tail_app_log" in script.tool_names
    assert "ops__run_scheduled_job" not in script.tool_names


def test_agents_follow_organization_visibility(client):
    org_id = client.post(f"{API}/orgs", json={"name": "hr-team"}, headers=ADMIN).json()["id"]
    _running_app("hr-agent", org_id)
    _running_app("public-agent")
    member = _user(client, "member@corp.com", org_id)
    outsider = _user(client, "outsider@corp.com")
    assert {a["name"] for a in client.get(f"{API}/smartwork/agents", headers=member).json()} \
        == {"hr-agent", "public-agent"}
    assert [a["name"] for a in client.get(f"{API}/smartwork/agents", headers=outsider).json()] \
        == ["public-agent"]


def _workflow(org_id: int, name: str, nodes: list[dict], waiting: bool = False) -> None:
    with SessionLocal() as db:
        row = Workflow(organization_id=org_id, name=name, description=f"{name} 절차",
                       spec={"nodes": nodes, "edges": []},
                       extracted={"constraints": [{"text": "500만원 넘으면 팀장 승인"}]})
        db.add(row)
        db.flush()
        if waiting:
            db.add(WorkflowRun(workflow_id=row.id, status=WorkflowRunStatus.waiting,
                               pending_node="approve"))
        db.commit()


def test_chat_opens_with_task_suggestions_from_department_workflows(client, monkeypatch):
    org_id = client.post(f"{API}/orgs", json={"name": "finance"}, headers=ADMIN).json()["id"]
    other = client.post(f"{API}/orgs", json={"name": "sales"}, headers=ADMIN).json()["id"]
    _workflow(org_id, "예산 집행", [
        {"id": "find", "type": "docs.search", "label": "예산안 찾기"},
        {"id": "approve", "type": "human", "title": "집행 승인", "role": "팀장"},
    ], waiting=True)
    _workflow(other, "견적 발송", [{"id": "q", "type": "human", "title": "견적 작성"}])
    alice = _user(client, "alice@corp.com", org_id)
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    _upload(client, alice, "work", {"report.md": b"# weekly\n\nreport"})
    tasks = [
        {"title": "예산 집행 승인", "prompt": "멈춰 있는 예산 집행 건을 승인 준비해 줘",
         "workflow": "예산 집행", "why": "report.md"},
        # 다른 부서의 워크플로 이름은 근거로 남지 않는다
        {"title": "견적", "prompt": "견적 보내 줘", "workflow": "견적 발송", "why": ""},
    ]
    out, script = _chat(client, alice, monkeypatch,
                        [("dept__workflows", {}), ("my__recent", {}),
                         ("suggest_tasks", {"tasks": tasks})], messages=[])
    assert script.messages[1] == {"role": "user", "content": smartwork.OPENING_PROMPT}
    assert "- 예산 집행" in script.messages[0]["content"]  # 시스템 프롬프트에 부서 워크플로
    dept = json.loads(script.results["dept__workflows"])
    assert [w["name"] for w in dept] == ["예산 집행"]  # 남의 부서 것은 없다
    assert dept[0]["steps"] == ["문서 검색: 예산안 찾기", "사람 작업: 집행 승인 (팀장)"]
    assert dept[0]["waiting_runs"] == 1
    assert dept[0]["constraints"] == ["500만원 넘으면 팀장 승인"]
    # 대시보드는 대화와 같은 목록을 본다
    assert client.get(f"{API}/smartwork/workflows", headers=alice).json() == dept
    assert json.loads(script.results["my__recent"])[0]["path"] == "work/report.md.md"
    assert out["suggestions"][0] == tasks[0]
    assert out["suggestions"][1]["workflow"] == ""

    # 제안(assistant)으로 시작하는 대화에도 여는 지시가 앞에 되살아난다
    _, script = _chat(client, alice, monkeypatch, [], messages=[
        {"role": "assistant", "content": "안녕하세요"},
        {"role": "user", "content": "첫 번째로 할게"},
    ])
    assert [m["role"] for m in script.messages] == ["system", "user", "assistant", "user"]

    # 소속이 없으면 부서 워크플로도 없다
    loner = _user(client, "loner@corp.com")
    _, script = _chat(client, loner, monkeypatch, [("dept__workflows", {})], messages=[])
    assert json.loads(script.results["dept__workflows"]) == []


def test_outlook_mail_comes_from_the_browser(client, monkeypatch):
    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    assert client.get(f"{API}/smartwork/personal", headers=alice).json()["mail"]["configured"] is False

    monkeypatch.setenv("PAAS_MS_GRAPH_CLIENT_ID", "client-123")
    get_settings.cache_clear()
    mail = client.get(f"{API}/smartwork/personal", headers=alice).json()["mail"]
    # 브라우저가 로그인할 때 쓰는 값 — 비밀이 아니다
    assert mail["configured"] and mail["client_id"] == "client-123"
    assert mail["tenant"] == "organizations" and mail["connected"] is False

    body = {"account": "alice@corp.com", "messages": [{
        "id": "m1", "subject": "예산 승인 요청", "receivedDateTime": "2026-10-07T09:00:00Z",
        "from": {"emailAddress": {"name": "김팀장", "address": "kim@corp.com"}},
        "toRecipients": [], "body": {"content": "펠리컨 예산안을 검토해 주세요"},
    }]}
    r = client.post(f"{API}/smartwork/personal/mail/messages", json=body, headers=alice)
    assert r.json() == {"fetched": 1, "new": 1}

    store = personal.store_for("alice@corp.com")
    hit = docsearch.search(store.name, "펠리컨")["hits"][0]
    assert hit["path"].startswith("outlook/2026-10-07_")
    assert "김팀장" in (store.root / hit["path"]).read_text(encoding="utf-8")

    # 이미 받은 메일은 다시 쓰지 않는다
    again = client.post(f"{API}/smartwork/personal/mail/messages", json=body, headers=alice).json()
    assert again == {"fetched": 1, "new": 0}
    mail = client.get(f"{API}/smartwork/personal", headers=alice).json()["mail"]
    assert mail["connected"] and mail["account"] == "alice@corp.com"
    # 서버에는 메일 토큰을 둘 자리 자체가 없다
    assert not any("token" in c for c in PersonalContext.__table__.columns.keys())

    assert client.delete(f"{API}/smartwork/personal/mail", headers=alice).status_code == 204
    assert not (store.root / "outlook").exists()
    assert docsearch.search(store.name, "펠리컨")["hits"] == []
    assert client.get(f"{API}/smartwork/personal", headers=alice).json()["mail"]["connected"] is False
