"""스마트워크 — 개인 업무 맥락(동의·폴더 동기화·메일·철회)과 대화(도구·화면 선택).

LLM은 chat_completion을 대본으로 바꿔 끼워 **도구 호출을 실제로 실행**시킨다 — 개인
도구가 남의 저장소를 못 보는지는 도구가 돌아야 확인된다. 메일은 브라우저가 Graph에서
읽어 보내므로 서버 쪽 시험에는 Graph가 없다.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import (
    Deployment, DeploymentStatus, PersonalContext, Project, Workflow, WorkflowRun,
    WorkflowRunStatus,
)
from app.services import docsearch, gitea, llm, msgraph, personal, smartwork

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
    plan = c.post(f"{API}/smartwork/personal/folders/manifest", params={"folder": folder},
                  json={"entries": entries}, headers=headers)
    assert plan.status_code == 200, plan.text
    # 브라우저처럼 needed를 하나씩, 본문 그대로 올린다
    for p in plan.json()["needed"]:
        r = c.put(f"{API}/smartwork/personal/folders/file",
                  params={"folder": folder, "path": p, "mtime": mtime}, content=files[p], headers=headers)
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


def _session(c, headers, **body) -> int:
    r = c.post(f"{API}/smartwork/sessions", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _chat(c, headers, monkeypatch, calls, text="질문", sid=None, folders=None, mail=False):
    """세션 하나에 한 턴. sid가 없으면 새 세션, folders·mail을 주면 내 맥락으로 고른다."""
    script = Script(calls)
    monkeypatch.setattr(llm, "chat_completion", script)
    sid = sid or _session(c, headers)
    if folders is not None or mail:
        r = c.put(f"{API}/smartwork/sessions/{sid}/context",
                  json={"folders": folders or [], "mail": mail}, headers=headers)
        assert r.status_code == 200, r.text
    r = c.post(f"{API}/smartwork/sessions/{sid}/messages", json={"content": text}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json(), script


def test_ask_user_offers_choices_before_running(client, monkeypatch):
    """도구가 여럿이면 실행 전에 묻는다 — 선택지는 버튼으로, 누르면 prompt가 다음 요청이다."""
    alice = _user(client, "alice@corp.com")
    out, script = _chat(client, alice, monkeypatch, [("ask_user", {
        "question": "어느 자료로 정리할까요?",
        "options": [{"label": "사내 문서", "prompt": "사내 문서로 정리해 줘"},
                    {"label": "내 메일", "prompt": "내 메일로 정리해 줘"},
                    {"label": "", "prompt": "버려진다"}],
    })], text="지난주 계약 진행 정리해 줘")
    assert out["choices"] == [{"label": "사내 문서", "prompt": "사내 문서로 정리해 줘"},
                              {"label": "내 메일", "prompt": "내 메일로 정리해 줘"}]
    assert out["report"] is None
    system = script.messages[0]["content"]
    assert "ask_user" in system and "HTML 보고서" in system


def test_attachments_are_reference_material_for_the_request(client, monkeypatch):
    import base64
    alice = _user(client, "alice@corp.com")
    sid = _session(client, alice)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    script = Script([])
    monkeypatch.setattr(llm, "chat_completion", script)
    r = client.post(f"{API}/smartwork/sessions/{sid}/messages", headers=alice, json={
        "content": "이 견적 검토해 줘",
        "attachments": [
            {"name": "견적.md", "type": "text/markdown",
             "data": base64.b64encode("# 견적\n금액 3천만원".encode()).decode()},
            {"name": "image.png", "type": "image/png", "data": png},
        ]})
    assert r.status_code == 200, r.text
    # 이번 턴: 문서는 글로, 이미지는 그림으로 모델에게 간다.
    last = script.messages[-1]
    assert last["role"] == "user"
    assert "금액 3천만원" in last["content"][0]["text"]
    assert last["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    # 대화에는 첨부의 이름과 읽어 낸 글만 남는다 — 이미지는 남기지 않는다.
    mine = client.get(f"{API}/smartwork/sessions/{sid}", headers=alice).json()["messages"][0]
    assert [(a["name"], a["kind"]) for a in mine["attachments"]] == [
        ("견적.md", "document"), ("image.png", "image")]
    assert mine["attachments"][1]["text"] == ""

    # 다음 턴에도 문서 글은 다시 읽히고, 이미지는 이름만 남는다.
    r = client.post(f"{API}/smartwork/sessions/{sid}/messages", headers=alice,
                    json={"content": "금액만 다시"})
    assert r.status_code == 200, r.text
    earlier = script.messages[1]["content"]
    assert "금액 3천만원" in earlier and "[첨부 이미지: image.png]" in earlier
    assert isinstance(script.messages[-1]["content"], str)

    # 문서·이미지가 아닌 것, 깨진 내용은 받지 않는다.
    for bad in ({"name": "setup.exe", "type": "", "data": png},
                {"name": "a.md", "type": "", "data": "%%%"}):
        r = client.post(f"{API}/smartwork/sessions/{sid}/messages", headers=alice,
                        json={"content": "이것도", "attachments": [bad]})
        assert r.status_code == 422, r.text


def test_personal_context_requires_consent(client):
    alice = _user(client, "alice@corp.com")
    status = client.get(f"{API}/smartwork/personal", headers=alice).json()
    assert status["consented"] is False
    r = client.post(f"{API}/smartwork/personal/folders/manifest", params={"folder": "work"},
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
    _upload(client, bob, "work", {"note.md": b"# note\n\nbob only"})

    sid = _session(client, alice)
    out, script = _chat(client, alice, monkeypatch, [("my__search", {"query": "zebra-42"})],
                        sid=sid, folders=["work"])
    assert "work/memo.md" in script.results["my__search"]

    # 같은 세션을 공유받아도 bob이 말하면 개인 도구는 bob의 저장소에 묶인다 — 고를 인자가 없다
    assert client.post(f"{API}/smartwork/sessions/{sid}/members",
                       json={"email": "bob@corp.com"}, headers=alice).status_code == 204
    _, script = _chat(client, bob, monkeypatch, [("my__search", {"query": "zebra-42"}),
                                                 ("my__read", {"path": "work/memo.md"})],
                      sid=sid, folders=["work"])
    assert json.loads(script.results["my__search"])["hits"] == []
    assert "파일이 없습니다" in script.results["my__read"]
    # 경로로 빠져나가지도 못한다
    _, script = _chat(client, bob, monkeypatch, [("my__read", {"path": "../"})], sid=sid)
    assert "도구 오류" in script.results["my__read"]
    # 남의 폴더 선택은 bob에게 보이지 않는다
    seen = client.get(f"{API}/smartwork/sessions/{sid}", headers=bob).json()
    assert seen["my_context"] == {"folders": ["work"], "mail": False}
    assert all(set(m) == {"email", "is_owner"} for m in seen["members"])

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
        # 형식을 무엇으로 적어 보내든 보고서는 HTML이다.
        ("show_report", {"title": "요약", "format": "md", "content": "<h1>요약</h1>"}),
    ], text="휴가 신청하고 싶어")
    assert out["agent"]["name"] == "leave-agent"
    assert out["agent"]["path"] == "/apps/_/leave-agent/"
    assert out["report"] == {"title": "요약", "format": "html", "content": "<h1>요약</h1>"}
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
                       extracted={
                           "constraints": [{"text": "500만원 넘으면 팀장 승인"}],
                           "entities": [{"name": "집행 요청", "note": "예산을 쓰겠다는 건"}],
                           "states": [{"entity": "집행 요청", "name": "접수"},
                                      {"entity": "집행 요청", "name": "승인"}],
                           "transitions": [{"from": "접수", "to": "승인", "trigger": "팀장 결재"}],
                       })
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
        {"title": "예산 집행 승인", "prompt": "3분기 장비 구매 집행 요청을 승인 준비해 줘",
         "target": {"kind": "집행 요청", "name": "3분기 장비 구매", "state": "접수"},
         "workflow": "예산 집행", "why": "report.md"},
        # 다른 부서의 워크플로 이름은 근거로 남지 않는다
        {"title": "견적", "prompt": "B사 견적 보내 줘", "target": {"kind": "견적", "name": "B사 견적"},
         "workflow": "견적 발송", "why": ""},
        # 진행할 대상이 없는 제안은 버린다 — 워크플로만으로는 할 일이 아니다
        {"title": "예산 집행", "prompt": "예산 집행해 줘", "workflow": "예산 집행"},
        {"title": "예산 집행", "prompt": "예산 집행해 줘", "target": {"kind": "집행 요청", "name": " "}},
    ]
    sid = _session(client, alice)
    out, script = _chat(client, alice, monkeypatch,
                        [("dept__workflows", {}), ("my__recent", {}),
                         ("suggest_tasks", {"tasks": tasks})], text="", sid=sid, folders=["work"])
    assert script.messages[1] == {"role": "user", "content": smartwork.OPENING_PROMPT}
    assert "- 예산 집행" in script.messages[0]["content"]  # 시스템 프롬프트에 부서 워크플로
    dept = json.loads(script.results["dept__workflows"])
    assert [w["name"] for w in dept] == ["예산 집행"]  # 남의 부서 것은 없다
    assert dept[0]["steps"] == ["문서 검색: 예산안 찾기", "사람 작업: 집행 승인 (팀장)"]
    assert dept[0]["waiting_runs"] == 1
    assert dept[0]["constraints"] == ["500만원 넘으면 팀장 승인"]
    # 제안이 찾을 대상의 종류와 그 상태 — 워크플로 대화에서 읽어 둔 업무 단위다
    assert dept[0]["targets"] == [{"kind": "집행 요청", "note": "예산을 쓰겠다는 건",
                                   "states": ["접수", "승인"]}]
    assert dept[0]["transitions"] == ["접수 → 승인 (팀장 결재)"]
    # 대시보드는 대화와 같은 목록을 본다
    assert client.get(f"{API}/smartwork/workflows", headers=alice).json() == dept
    assert json.loads(script.results["my__recent"])[0]["path"] == "work/report.md.md"
    assert out["suggestions"][0] == tasks[0]
    assert out["suggestions"][1]["workflow"] == ""
    assert out["suggestions"][1]["target"] == {"kind": "견적", "name": "B사 견적", "state": ""}
    assert len(out["suggestions"]) == 2

    # 제안(assistant)으로 시작하는 대화에도 여는 지시가 앞에 되살아난다
    _, script = _chat(client, alice, monkeypatch, [], text="첫 번째로 할게", sid=sid)
    assert [m["role"] for m in script.messages] == ["system", "user", "assistant", "user"]
    assert script.messages[1]["content"] == smartwork.OPENING_PROMPT
    # 여는 턴은 대화가 없을 때만이다
    r = client.post(f"{API}/smartwork/sessions/{sid}/messages", json={"content": ""}, headers=alice)
    assert r.status_code == 422
    # 첫 요청이 제목이 된다
    assert client.get(f"{API}/smartwork/sessions/{sid}", headers=alice).json()["title"] \
        == "첫 번째로 할게"

    # 소속이 없으면 부서 워크플로도 없다
    loner = _user(client, "loner@corp.com")
    _, script = _chat(client, loner, monkeypatch, [("dept__workflows", {})], text="")
    assert json.loads(script.results["dept__workflows"]) == []


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def test_outlook_mail_login_by_device_code_keeps_no_token(client, monkeypatch):
    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    assert client.get(f"{API}/smartwork/personal", headers=alice).json()["mail"]["configured"] is False
    assert client.post(f"{API}/smartwork/personal/mail/login", headers=alice).status_code == 502

    monkeypatch.setenv("PAAS_MS_GRAPH_CLIENT_ID", "client-123")
    get_settings.cache_clear()
    assert client.get(f"{API}/smartwork/personal", headers=alice).json()["mail"]["configured"] is True

    inbox = {"value": [{
        "id": "m1", "subject": "예산 승인 요청", "receivedDateTime": "2026-10-07T09:00:00Z",
        "from": {"emailAddress": {"name": "김팀장", "address": "kim@corp.com"}},
        "toRecipients": [], "body": {"content": "펠리컨 예산안을 검토해 주세요"},
    }]}
    approved = {"yes": False}
    sent: list[tuple[str, dict]] = []

    def post(url, data=None, **kw):
        sent.append((url, data))
        if url.endswith("/devicecode"):
            return _Resp(200, {"device_code": "dc-secret", "user_code": "ABC123", "expires_in": 900,
                               "interval": 5, "verification_uri": "https://login.microsoft.com/device"})
        if not approved["yes"]:
            return _Resp(400, {"error": "authorization_pending"})
        return _Resp(200, {"access_token": "tok-secret", "expires_in": 3600})

    def get(url, **kw):
        assert kw["headers"]["Authorization"] == "Bearer tok-secret"
        return _Resp(200, {"mail": "alice@corp.com"} if url.endswith("/me") else inbox)

    monkeypatch.setattr(msgraph.httpx, "post", post)
    monkeypatch.setattr(msgraph.httpx, "get", get)

    login = client.post(f"{API}/smartwork/personal/mail/login", headers=alice).json()
    # 사람이 쓸 것만 나간다 — device_code는 서버에 남는다
    assert login == {"verification_url": "https://login.microsoft.com/device", "user_code": "ABC123",
                     "expires_in": 900, "interval": 5}
    assert "offline_access" not in sent[0][1]["scope"]  # refresh token을 받지 않는다

    poll = f"{API}/smartwork/personal/mail/login/poll"
    assert client.post(poll, headers=alice).json() == {"status": "pending"}
    approved["yes"] = True
    done = client.post(poll, headers=alice).json()
    assert done == {"status": "done", "account": "alice@corp.com", "fetched": 1, "new": 1}
    assert "tok-secret" not in str(msgraph._pending)
    # 한 번 끝난 로그인은 다시 쓸 수 없다
    assert client.post(poll, headers=alice).status_code == 502

    store = personal.store_for("alice@corp.com")
    hit = docsearch.search(store.name, "펠리컨")["hits"][0]
    assert hit["path"].startswith("outlook/2026-10-07_")
    assert "김팀장" in (store.root / hit["path"]).read_text(encoding="utf-8")
    mail = client.get(f"{API}/smartwork/personal", headers=alice).json()["mail"]
    assert mail["connected"] and mail["account"] == "alice@corp.com"
    # 서버에는 메일 토큰을 둘 자리 자체가 없다
    assert not any("token" in c for c in PersonalContext.__table__.columns.keys())

    assert client.delete(f"{API}/smartwork/personal/mail", headers=alice).status_code == 204
    assert not (store.root / "outlook").exists()
    assert docsearch.search(store.name, "펠리컨")["hits"] == []
    assert client.get(f"{API}/smartwork/personal", headers=alice).json()["mail"]["connected"] is False


def test_outlook_mail_login_needs_consent_and_names_the_app_setting(client, monkeypatch):
    alice = _user(client, "alice@corp.com")
    monkeypatch.setenv("PAAS_MS_GRAPH_CLIENT_ID", "client-123")
    get_settings.cache_clear()
    assert client.post(f"{API}/smartwork/personal/mail/login", headers=alice).status_code == 400

    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    monkeypatch.setattr(msgraph.httpx, "post", lambda *a, **k: _Resp(400, {
        "error": "invalid_client", "error_description": "AADSTS7000218: The request body must contain"}))
    r = client.post(f"{API}/smartwork/personal/mail/login", headers=alice)
    assert r.status_code == 502 and "공용 클라이언트 흐름 허용" in r.json()["detail"]


def test_expired_sso_starts_the_login_and_hands_the_url_to_the_user(client, monkeypatch):
    """SSO 만료는 사람이 '허용'을 눌러야 풀린다 — 그 앞의 로그인 시작은 서버가 바로 한다.
    승인 주소는 실패한 요청을 낸 사람(일반 사용자)의 화면으로 간다."""
    from app.services import bedrock

    alice = _user(client, "alice@corp.com")
    started: list[str] = []

    def expired(*args, **kwargs):
        raise bedrock.SsoExpired("bedrock-dev")

    def start(profile, log_dir):
        started.append(profile)
        return {"profile": profile, "code_autofilled": True, "user_code": "AAAA-BBBB",
                "verification_url": "https://d-1.awsapps.com/start/#/device?user_code=AAAA-BBBB",
                "log_path": "", "log_tail": "server log"}

    monkeypatch.setattr(llm, "chat_completion", expired)
    monkeypatch.setattr(bedrock, "start_sso_login", start)
    sid = _session(client, alice)
    r = client.post(f"{API}/smartwork/sessions/{sid}/messages", json={"content": "질문"},
                    headers=alice)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert started == ["bedrock-dev"]
    assert "만료" in detail["message"]
    assert detail["sso_login"]["verification_url"].endswith("user_code=AAAA-BBBB")
    assert "log_tail" not in detail["sso_login"]  # 서버 로그는 일반 사용자에게 내보내지 않는다
    assert client.get(f"{API}/smartwork/sessions/{sid}", headers=alice).json()["messages"] == []


def test_session_is_shared_for_talking_but_managed_by_its_owner(client, monkeypatch):
    """공유받은 사람(다른 부서여도)은 읽고 말한다. 맥락·참여자·삭제는 소유자만 한다."""
    finance = client.post(f"{API}/orgs", json={"name": "finance"}, headers=ADMIN).json()["id"]
    sales = client.post(f"{API}/orgs", json={"name": "sales"}, headers=ADMIN).json()["id"]
    alice = _user(client, "alice@corp.com", finance)
    bob = _user(client, "bob@corp.com", sales)
    carol = _user(client, "carol@corp.com")
    sid = _session(client, alice, organization_id=finance)
    base = f"{API}/smartwork/sessions/{sid}"

    # 공유 전에는 있는지도 모른다
    assert client.get(base, headers=bob).status_code == 404
    assert client.post(f"{base}/members", json={"email": "nobody@corp.com"},
                       headers=alice).status_code == 404  # 승인된 계정만
    assert client.post(f"{base}/members", json={"email": "bob@corp.com"},
                       headers=alice).status_code == 204

    _chat(client, alice, monkeypatch, [], text="예산안 정리하자", sid=sid)
    seen = client.get(base, headers=bob).json()
    assert seen["is_owner"] is False and seen["organization"] == "finance"
    assert [m["email"] for m in seen["members"]] == ["alice@corp.com", "bob@corp.com"]
    last = seen["messages"][-1]["id"]

    # bob도 말한다 — 모델은 누가 말했는지 안다
    out, script = _chat(client, bob, monkeypatch, [], text="자료는 내가 찾을게", sid=sid)
    assert script.messages[-1]["content"] == "[bob@corp.com] 자료는 내가 찾을게"
    assert "지금 말하는 사람: bob@corp.com" in script.messages[0]["content"]
    assert "alice@corp.com, bob@corp.com" in script.messages[0]["content"]
    assert out["message"]["role"] == "assistant"
    # after로 새 메시지만 다시 읽는다
    fresh = client.get(base, params={"after": last}, headers=alice).json()["messages"]
    assert [(m["role"], m["author"]) for m in fresh] == [("user", "bob@corp.com"),
                                                          ("assistant", "bob@corp.com")]
    assert [s["members"] for s in client.get(f"{API}/smartwork/sessions", headers=bob).json()] == [2]

    # 소유자만 할 수 있는 일
    assert client.patch(base, json={"title": "x"}, headers=bob).status_code == 403
    assert client.post(f"{base}/members", json={"email": "carol@corp.com"},
                       headers=bob).status_code == 403
    assert client.delete(base, headers=bob).status_code == 403
    assert client.delete(f"{base}/members/alice@corp.com", headers=alice).status_code == 422
    assert client.get(base, headers=carol).status_code == 404
    assert client.post(f"{base}/messages", json={"content": "끼어들기"},
                       headers=carol).status_code == 404

    # 참여자는 스스로 나간다
    assert client.delete(f"{base}/members/bob@corp.com", headers=bob).status_code == 204
    assert client.get(base, headers=bob).status_code == 404
    assert client.delete(base, headers=alice).status_code == 204
    assert client.get(base, headers=alice).status_code == 404


def test_session_task_is_an_own_organization_and_its_workflow(client, monkeypatch):
    finance = client.post(f"{API}/orgs", json={"name": "finance"}, headers=ADMIN).json()["id"]
    sales = client.post(f"{API}/orgs", json={"name": "sales"}, headers=ADMIN).json()["id"]
    _workflow(finance, "예산 집행", [{"id": "approve", "type": "human", "title": "집행 승인"}])
    _workflow(sales, "견적 발송", [{"id": "q", "type": "human", "title": "견적 작성"}])
    alice = _user(client, "alice@corp.com", finance)

    orgs = client.get(f"{API}/smartwork/orgs", headers=alice).json()
    assert [(o["name"], [w["name"] for w in o["workflows"]]) for o in orgs] \
        == [("finance", ["예산 집행"])]
    budget = orgs[0]["workflows"][0]["id"]
    with SessionLocal() as db:
        quote = next(w.id for w in db.query(Workflow).all() if w.name == "견적 발송")

    def create(**body):
        return client.post(f"{API}/smartwork/sessions", json=body, headers=alice).status_code

    assert create(organization_id=sales) == 403  # 소속이 아닌 조직
    assert create(organization_id=finance, workflow_id=quote) == 422  # 남의 조직 워크플로
    assert create(workflow_id=budget) == 422  # 조직 없이 워크플로만

    sid = _session(client, alice, organization_id=finance, workflow_id=budget)
    _, script = _chat(client, alice, monkeypatch, [], sid=sid)
    system = script.messages[0]["content"]
    assert "이 대화의 업무: 조직 finance / 워크플로 예산 집행" in system
    assert "- 단계: 사람 작업: 집행 승인" in system
    assert "- 규칙: 500만원 넘으면 팀장 승인" in system

    # 조직을 비우면 그 조직의 워크플로도 빠진다
    r = client.patch(f"{API}/smartwork/sessions/{sid}", json={"organization_id": None},
                     headers=alice)
    assert r.status_code == 200 and r.json()["workflow_id"] is None
    _, script = _chat(client, alice, monkeypatch, [], sid=sid)
    assert "이 대화의 업무" not in script.messages[0]["content"]


def test_session_sees_only_the_folders_chosen_for_it(client, monkeypatch):
    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    _upload(client, alice, "work", {"plan.md": "# 계획\n\n오로라 일정".encode()})
    _upload(client, alice, "home", {"diary.md": "# 일기\n\n펭귄 여행".encode()})
    sid = _session(client, alice)

    # 고르지 않으면 개인 도구가 없다
    _, script = _chat(client, alice, monkeypatch, [], sid=sid)
    assert not any(n.startswith("my__") for n in script.tool_names)

    # 없는 폴더, 받지 않은 메일은 고를 수 없다
    r = client.put(f"{API}/smartwork/sessions/{sid}/context",
                   json={"folders": ["work", "ghost"], "mail": True}, headers=alice)
    assert r.json() == {"folders": ["work"], "mail": False}
    _, script = _chat(client, alice, monkeypatch, [
        ("my__search", {"query": "펭귄"}), ("my__read", {"path": "home/diary.md.md"}),
        ("my__sources", {}), ("my__recent", {}),
    ], sid=sid)
    assert json.loads(script.results["my__search"])["hits"] == []
    assert "고른 폴더" in script.results["my__read"]
    assert [f["name"] for f in json.loads(script.results["my__sources"])["folders"]] == ["work"]
    assert [d["path"] for d in json.loads(script.results["my__recent"])] == ["work/plan.md.md"]


def test_folder_name_travels_in_the_query_not_the_path(client):
    """PC 폴더 이름에는 한글·&·%가 흔하다 — URL 경로에 넣으면 IIS/ARR 앞단이 사유 없이
    400으로 막았다. 쿼리로 받아 그대로 폴더 이름이 되어야 한다(지울 때도 같다)."""
    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    name = "1.계약&품의 50%"
    _upload(client, alice, name, {"하위 폴더/검토 v1.md": "# 계약 검토".encode()})
    status = client.get(f"{API}/smartwork/personal", headers=alice).json()
    assert [f["name"] for f in status["folders"]] == [name]
    r = client.delete(f"{API}/smartwork/personal/folders", params={"folder": name}, headers=alice)
    assert r.status_code == 204
    assert client.get(f"{API}/smartwork/personal", headers=alice).json()["folders"] == []


def test_file_upload_streams_one_file_and_stops_at_the_cap(client, monkeypatch):
    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    monkeypatch.setattr(personal, "MAX_FILE_BYTES", 16)
    url = f"{API}/smartwork/personal/folders/file"

    # 길이를 미리 밝히면 받기 전에 끊는다
    r = client.put(url, params={"folder": "work", "path": "big.md"}, content=b"x" * 17, headers=alice)
    assert r.status_code == 413
    # 길이 없이(조각으로) 흘려 보내도 상한에서 끊는다
    r = client.put(url, params={"folder": "work", "path": "big.md"}, content=iter([b"x" * 10, b"x" * 10]),
                   headers=alice)
    assert r.status_code == 413
    store = personal.store_for("alice@corp.com")
    assert not store.root.exists() or not any(p.is_file() for p in store.root.rglob("*"))

    r = client.put(url, params={"folder": "work", "path": "ok.md", "mtime": 1_700_000_000}, content=b"# ok\n\nfine",
                   headers=alice)
    assert r.json() == {"path": "ok.md", "status": "saved"}
    assert (store.root / "work/ok.md.md").is_file()


def test_conversation_runs_on_the_chosen_provider_within_its_organization(client, monkeypatch):
    finance = client.post(f"{API}/orgs", json={"name": "finance"}, headers=ADMIN).json()["id"]
    sales = client.post(f"{API}/orgs", json={"name": "sales"}, headers=ADMIN).json()["id"]

    def provider(name, org=None):
        r = client.post(f"{API}/llm/providers", json={
            "name": name, "kind": "openai", "base_url": "https://api.example.com",
            "api_key": "sk-secret", "model": f"{name}-model", "organization_id": org}, headers=ADMIN)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    default_id = client.get(f"{API}/llm/providers", headers=ADMIN).json()[0]["id"]
    client.post(f"{API}/llm/providers/{default_id}/default", headers=ADMIN)
    global_pick, finance_only, sales_only = provider("fast"), provider("fin", finance), provider("sal", sales)
    used: list[str] = []
    monkeypatch.setattr(llm, "chat_completion",
                        lambda p, *a, **k: used.append(p.name) or "완료")
    alice = _user(client, "alice@corp.com", finance)
    sid = _session(client, alice, organization_id=finance)

    def send(provider_id=None):
        return client.post(f"{API}/smartwork/sessions/{sid}/messages",
                           json={"content": "질문", "provider_id": provider_id}, headers=alice)

    assert send().status_code == 200 and used[-1] == "claude"      # 안 고르면 기본
    assert send(global_pick).status_code == 200 and used[-1] == "fast"
    assert send(finance_only).status_code == 200 and used[-1] == "fin"
    assert send(sales_only).status_code == 403                      # 다른 조직 전용 모델
    assert send(9999).status_code == 404
