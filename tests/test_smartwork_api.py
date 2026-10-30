"""스마트워크 — 개인 업무 맥락(동의·폴더 동기화·메일·철회)과 대화(도구·화면 선택).

LLM은 chat_completion을 대본으로 바꿔 끼워 **도구 호출을 실제로 실행**시킨다 — 개인
도구가 남의 저장소를 못 보는지는 도구가 돌아야 확인된다. 메일은 브라우저가 Graph에서
읽어 보내므로 서버 쪽 시험에는 Graph가 없다.
"""
import json
import subprocess
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
from app.services import docsearch, doctext, gitea, llm, msgraph, personal, smartwork

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
        self.finish_tools: list[str] = []

    def __call__(self, provider, messages, db=None, tools=None, tool_executor=None,
                 finish_tools=()):
        self.messages = messages
        self.finish_tools = list(finish_tools)
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
    # 결과는 대화가 아니라 결과 화면으로 — show_report는 보고서 요청만이 아니라 답의 기본 형식이다.
    assert "ask_user" in system and "show_report가 답의 기본 형식" in system


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

    # 다시 골라도 같은 파일은 다시 올리지 않는다 — 바뀐 파일(크기·수정 시각)과 새 파일만
    assert _upload(client, alice, "work", files)["needed"] == []
    assert _upload(client, alice, "work", files, mtime=1_700_000_100.0)["needed"] == ["plan/q3.md"]
    more = {**files, "plan/q4.md": "# 4분기".encode()}
    assert _upload(client, alice, "work", more, mtime=1_700_000_100.0)["needed"] == ["plan/q4.md"]
    files = more

    # 저장할 수 없는 경로라 건너뛴 파일도 기록한다 — 남기지 않으면 동기화마다 다시 받는다
    real = personal.storage_service.resolve

    def refuse(root, rel):
        if "q5" in rel:
            raise personal.storage_service.StorageError("경로가 너무 깁니다")
        return real(root, rel)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(personal.storage_service, "resolve", refuse)
        odd = {**files, "plan/q5.md": "# 5".encode()}
        assert _upload(client, alice, "work", odd, mtime=1_700_000_100.0)["needed"] == ["plan/q5.md"]
        assert _upload(client, alice, "work", odd, mtime=1_700_000_100.0)["needed"] == []
    files = {k: v for k, v in odd.items() if k != "plan/q4.md" and k != "plan/q5.md"}
    assert _upload(client, alice, "work", files, mtime=1_700_000_100.0)["removed"] == 2
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
    # 개발자 .env에 값이 있어도 "미설정"에서 시작한다.
    monkeypatch.setenv("PAAS_MS_GRAPH_CLIENT_ID", "")
    get_settings.cache_clear()
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


def test_web_outlook_mail_from_the_bookmark_goes_to_the_owner_only(client):
    """Graph 권한 없이 — 웹 Outlook 북마크가 넘긴 화면의 글자를 그 사람의 저장소(outlook/)에만 넣는다."""
    alice = _user(client, "alice@corp.com")
    bob = _user(client, "bob@corp.com")
    url = f"{API}/smartwork/personal/mail/web"
    item = {"subject": "협력사 단가 협의", "header": "김팀장 <kim@corp.com>  2026-10-20 (월) 오전 9:12  받는 사람: alice",
            "body": "오리온 부품 단가를 3% 낮추는 안입니다."}
    assert client.post(url, json={"items": [item]}, headers=alice).status_code == 400   # 동의 전

    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    client.post(f"{API}/smartwork/personal/consent", headers=bob)
    out = client.post(url, json={"items": [item, {"subject": "빈 것"}]}, headers=alice).json()
    assert out["new"] == 1 and out["saved"][0]["path"].startswith("outlook/web_")
    # 같은 메일을 다시 누르면 새로 쓰지 않는다
    assert client.post(url, json={"items": [item]}, headers=alice).json()["new"] == 0

    store = personal.store_for("alice@corp.com")
    hit = docsearch.search(store.name, "오리온")["hits"][0]
    text = (store.root / hit["path"]).read_text(encoding="utf-8")
    assert text.startswith("# 협력사 단가 협의") and "김팀장" in text
    assert client.get(f"{API}/smartwork/personal", headers=alice).json()["mail"]["web"] == 1
    # 웹 Outlook으로 넣은 메일만 있어도 업무에 「메일」을 고를 수 있다(Graph 동기화 없이)
    sid = _session(client, alice)
    ctx = client.put(f"{API}/smartwork/sessions/{sid}/context", json={"folders": [], "mail": True}, headers=alice)
    assert ctx.status_code == 200 and ctx.json()["mail"] is True, ctx.text
    assert docsearch.search(personal.store_for("bob@corp.com").name, "오리온")["hits"] == []

    from app.db import SessionLocal
    from app.models import AuditEvent
    with SessionLocal() as s:
        event = s.query(AuditEvent).filter(AuditEvent.action == "smartwork.personal.mail.web").first()
        assert "오리온" not in json.dumps(event.detail, ensure_ascii=False)   # 내용은 감사에 남지 않는다
    # 메일 지우기는 웹 Outlook에서 넣은 것도 지운다
    assert client.delete(f"{API}/smartwork/personal/mail", headers=alice).status_code == 204
    assert docsearch.search(store.name, "오리온")["hits"] == []


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
    # 관리자는 모든 조직의 업무와 워크플로를 고른다(워크플로 관리와 같은 규칙)
    admin_orgs = {o["name"]: [w["name"] for w in o["workflows"]]
                  for o in client.get(f"{API}/smartwork/orgs", headers=ADMIN).json()}
    assert admin_orgs["finance"] == ["예산 집행"] and admin_orgs["sales"] == ["견적 발송"]
    assert client.post(f"{API}/smartwork/sessions", json={"organization_id": sales, "workflow_id": quote},
                       headers=ADMIN).status_code == 201
    assert client.post(f"{API}/smartwork/sessions", json={"organization_id": 99999},
                       headers=ADMIN).status_code == 404
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
    # 관리자는 모든 조직의 프로바이더를 쓴다(다른 LLM 사용과 같은 규칙)
    admin_sid = _session(client, ADMIN)
    reply = client.post(f"{API}/smartwork/sessions/{admin_sid}/messages",
                        json={"content": "질문", "provider_id": sales_only}, headers=ADMIN)
    assert reply.status_code == 200 and used[-1] == "sal"


def test_eml_mail_files_are_searchable_and_not_kept(client):
    """Entra 앱 없이도 웹 Outlook에서 내려받은 .eml을 폴더로 올려 메일을 맥락으로 쓴다."""
    from email.message import EmailMessage

    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "예산 승인 요청", "kim@corp.com", "alice@corp.com"
    msg.set_content("펠리컨 예산안을 검토해 주세요")
    assert _upload(client, alice, "mail", {"a.eml": msg.as_bytes()})["needed"] == ["a.eml"]

    store = personal.store_for("alice@corp.com")
    assert docsearch.search(store.name, "펠리컨")["hits"][0]["path"] == "mail/a.eml.md"
    assert sorted(p.relative_to(store.root).as_posix() for p in store.root.rglob("*") if p.is_file()) \
        == ["mail/a.eml.md"]


def test_answer_rating_is_by_participants_and_the_last_press_wins(client, monkeypatch):
    """답변 평가는 그 세션 사람만, 한 사람에 하나 — 다시 누르면 덮어쓴다.

    여러 번 누른 사람이 집계를 끌면 모델 비교가 무의미해진다. 그리고 점수에는 **그때 답한
    모델**이 박혀야 한다 — 세션 모델은 나중에 바뀌므로 설정을 보면 알 수 없다.
    """
    alice = _user(client, "alice@corp.com")
    bob = _user(client, "bob@corp.com")
    sid = _session(client, alice)
    out, _ = _chat(client, alice, monkeypatch, [], text="예산안 정리하자", sid=sid)
    answer = out["message"]
    assert (answer["model"], answer["provider"]) == ("test-model", "claude")
    rate = f"{API}/smartwork/messages/{answer['id']}/rating"

    assert client.post(rate, json={"score": 1}, headers=alice).status_code == 200
    assert client.post(rate, json={"score": -1, "reason": "숫자가 틀렸다"},
                       headers=alice).json()["score"] == -1
    # 남의 세션 답변은 평가할 수 없고, 점수는 좋음·아쉬움 둘뿐이다
    assert client.post(rate, json={"score": 1}, headers=bob).status_code == 404
    assert client.post(rate, json={"score": 5}, headers=alice).status_code == 422
    assert client.post(f"{API}/smartwork/messages/9999/rating", json={"score": 1},
                       headers=alice).status_code == 404
    # 사람이 쓴 말에는 평가가 붙지 않는다
    messages = client.get(f"{API}/smartwork/sessions/{sid}", headers=alice).json()["messages"]
    assert messages[0]["role"] == "user"
    assert client.post(f"{API}/smartwork/messages/{messages[0]['id']}/rating",
                       json={"score": 1}, headers=alice).status_code == 404
    # 누른 상태가 대화에 보인다 — 같은 답을 또 평가하지 않게
    assert [m["my_rating"] for m in messages] == [0, -1]

    summary = client.get(f"{API}/telemetry/quality?days=30", headers=ADMIN).json()
    assert summary["totals"] == {"rated": 1, "good": 0, "poor": 1, "raters": 1}
    assert summary["by_model"] == [{"key": "claude / test-model", "rated": 1,
                                    "good": 0, "poor": 1}]
    assert summary["recent_poor"][0]["reason"] == "숫자가 틀렸다"
    # **누가 매겼는지는 내보내지 않는다** — 이름이 보이면 솔직한 평가가 줄어든다.
    assert "alice@corp.com" not in json.dumps(summary, ensure_ascii=False)
    assert client.get(f"{API}/telemetry/quality").status_code == 401


def test_file_viewer_opens_only_my_chosen_documents_and_edits_are_proposals(client, monkeypatch):
    """파일 뷰어는 이 업무에 고른 내 폴더의 문서만 연다 — 모델의 수정은 제안으로만 실린다.

    서버에는 원본이 없다. 목록은 동기화 때 적어 둔 경로이고, 내용은 브라우저가 PC의 파일로
    연다. 그래서 open_file이 하는 일은 "이 파일을 열어라"와 수정안을 화면에 싣는 것뿐이다.
    """
    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    _upload(client, alice, "work", {"memo.txt": "단가 120원".encode(), "plan.md": b"# x"})
    _upload(client, alice, "home", {"diary.txt": "펭귄".encode()})
    sid = _session(client, alice)

    # 고르기 전에는 열 것이 없고, open_file 도구도 붙지 않는다
    with SessionLocal() as db:
        assert smartwork.session_files(db, "alice@corp.com", []) == []
    _, script = _chat(client, alice, monkeypatch, [], sid=sid)
    assert "open_file" not in script.tool_names
    assert {"show_web", "make_file"} <= set(script.tool_names)

    client.put(f"{API}/smartwork/sessions/{sid}/context",
               json={"folders": ["work"], "mail": False}, headers=alice)
    # .md는 뷰어 형식이 아니다(보고서가 맡는다) — 목록에 없다. 목록은 사람에게 내보내지 않는다.
    with SessionLocal() as db:
        assert [(f["folder"], f["path"]) for f in smartwork.session_files(db, "alice@corp.com", ["work"])] == \
            [("work", "memo.txt")]
    assert client.get(f"{API}/smartwork/sessions/{sid}/files", headers=alice).status_code in (404, 405)

    out, script = _chat(client, alice, monkeypatch, [
        ("open_file", {"path": "work/memo.txt.md", "edits": [
            {"find": "120원", "replace": "150원", "why": "새 단가"},
            {"cell": "A1", "value": "x"},                # 글 파일에 셀 수정은 버린다
        ]}),
    ], sid=sid)
    assert out["file"] == {"source": "personal", "folder": "work", "path": "memo.txt",
                           "name": "memo.txt", "format": "txt",
                           "edits": [{"find": "120원", "replace": "150원", "why": "새 단가"}]}
    # 대화를 다시 열어도 그 자리의 파일이 남는다
    last = client.get(f"{API}/smartwork/sessions/{sid}", headers=alice).json()["messages"][-1]
    assert last["file"]["path"] == "memo.txt"


def test_open_file_refuses_paths_outside_the_session(client, monkeypatch):
    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    _upload(client, alice, "work", {"memo.txt": "a".encode()})
    _upload(client, alice, "home", {"diary.txt": "b".encode()})
    out, script = _chat(client, alice, monkeypatch,
                        [("open_file", {"path": "home/diary.txt"})], folders=["work"])
    assert "고른 내 문서가 아니" in script.results["open_file"]
    assert out["file"] is None


def test_make_file_and_show_web_are_checked_before_they_reach_the_screen(client, monkeypatch):
    """모델이 만든 파일은 형식·크기를 확인하고, 웹 주소는 http(s)만 — 사람 브라우저에서 열린다."""
    alice = _user(client, "alice@corp.com")
    out, script = _chat(client, alice, monkeypatch, [
        ("show_web", {"url": "javascript:alert(1)", "reason": "x"}),
        ("make_file", {"name": "a", "format": "docx", "content": "x"}),
        ("make_file", {"name": "협력사/명단", "format": "xlsx", "sheets": [
            {"name": "명단", "rows": [["이름", "점수"], ["가", "90"], ["나", None]]}]}),
    ])
    assert "http" in script.results["show_web"] and out["web"] is None
    assert out["file"] == {"source": "generated", "name": "협력사_명단.xlsx", "format": "xlsx",
                           "sheets": [{"name": "명단",
                                       "rows": [["이름", "점수"], ["가", "90"], ["나", ""]]}]}

    out, script = _chat(client, alice, monkeypatch, [
        ("show_web", {"url": "https://portal.corp.com/approve?id=7", "reason": "결재 화면"}),
        ("make_file", {"name": "초안", "format": "txt", "content": "x" * 300_000}),
    ])
    assert out["web"] == {"url": "https://portal.corp.com/approve?id=7", "reason": "결재 화면"}
    assert "너무 큽니다" in script.results["make_file"] and out["file"] is None


def test_office_preview_converts_and_keeps_nothing(client, monkeypatch, tmp_path):
    """doc·docx·ppt·pptx는 서버의 LibreOffice로 PDF를 만들어 돌려준다 — 원본은 남지 않는다."""
    alice = _user(client, "alice@corp.com")
    preview = f"{API}/smartwork/files/preview"
    seen: list = []

    def fake_run(cmd, **kw):
        source = Path(cmd[-1])
        seen.append(source)
        assert source.read_bytes() == b"DOCX"
        out_dir = Path(cmd[cmd.index("--outdir") + 1])
        (out_dir / (source.stem + ".pdf")).write_bytes(b"%PDF-1.7")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(doctext, "_soffice", lambda: "soffice")
    monkeypatch.setattr(doctext.subprocess, "run", fake_run)
    r = client.post(preview, params={"name": "보고서.docx"}, content=b"DOCX", headers=alice)
    assert r.status_code == 200 and r.content == b"%PDF-1.7"
    assert r.headers["content-type"] == "application/pdf"
    assert not seen[0].exists()  # 임시 폴더째 지워졌다

    assert client.post(preview, params={"name": "a.xlsx"}, content=b"x",
                       headers=alice).status_code == 400  # 엑셀은 브라우저가 직접 연다
    monkeypatch.setattr(doctext, "_soffice", lambda: None)
    r = client.post(preview, params={"name": "a.pptx"}, content=b"x", headers=alice)
    assert r.status_code == 503 and "LibreOffice" in r.json()["detail"]
    assert client.post(preview, params={"name": "a.docx"}, content=b"x").status_code == 401


def test_a_turn_that_ends_without_a_result_says_why(client, monkeypatch):
    """찾기만 하다 끝난 턴은 빈 말풍선이 아니라 이유를 남긴다 — 그래야 사람이 좁혀 다시 묻는다.

    왕복 한도를 넘기면 결과를 내는 도구만 돌게(llm.WRAP_UP_ROUNDS) 화면 도구 이름을 넘긴다.
    """
    alice = _user(client, "alice@corp.com")
    out, script = _chat(client, alice, monkeypatch, [("docs__search_docs", {"query": "x"})] * 3,
                        text="보고서 써 줘")
    assert out["reply"] == "완료"  # 짧은 답은 그대로
    # show_report가 기본 응답 형식이라 맨 앞이다 — 한도에 이르면 다른 화면이 맞지 않는 한 그것으로 낸다.
    assert script.finish_tools[0] == "show_report"
    assert {"ask_user", "make_file"} <= set(script.finish_tools)
    assert not any(n.startswith(("docs__", "my__")) for n in script.finish_tools)
    assert "도구 왕복은 최대" in script.messages[0]["content"]
    assert "답의 기본 형식" in script.messages[0]["content"]

    # 화면을 고르지 않고 결과를 대화에 길게 쓰고 끝냈으면 — 결과 화면(show_report)으로 받는다.
    long_answer = "# 협력사 3곳 비교\n\n" + "가 협력사는 단가가 낮다. " * 40
    script = Script([], reply=long_answer)
    monkeypatch.setattr(llm, "chat_completion", script)
    r = client.post(f"{API}/smartwork/sessions/{_session(client, alice)}/messages",
                    json={"content": "협력사 비교해 줘"}, headers=alice).json()
    assert r["report"] == {"title": "협력사 3곳 비교", "format": "md", "content": long_answer.strip()}
    assert r["reply"] == "협력사 3곳 비교 — 자세한 내용은 오른쪽 화면에 띄웠습니다."
    assert r["message"]["report"]["format"] == "md"  # 다시 열어도 그 화면이 남는다

    script = Script([("docs__search_docs", {"query": "x"})] * 3, reply="")
    monkeypatch.setattr(llm, "chat_completion", script)
    sid = _session(client, alice)
    r = client.post(f"{API}/smartwork/sessions/{sid}/messages", json={"content": "보고서"}, headers=alice)
    assert r.status_code == 200, r.text
    assert "결과를 내지 못했습니다 — 도구를 3번" in r.json()["reply"]
    assert "결과를 내지 못했습니다" in r.json()["message"]["content"]


def test_a_turn_records_where_its_input_came_from_and_what_it_used(client, monkeypatch):
    """작업 로그의 입력 구성 — 지시문 구역·대화 구역·자료 출처로 나뉘고, 읽은 자료가 결과에
    쓰였는지 센다. 실제 호출 경로(llm.chat_completion)를 그대로 지나고 응답만 가짜다.
    **경로·본문은 남지 않는다**(출처 이름과 크기·개수뿐)."""
    from app.models import LlmCallDetail, LlmTurn

    alice = _user(client, "alice@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    _upload(client, alice, "work", {"plan.md": "# 계획\n\n오로라 일정".encode(),
                                    "memo.md": "# 메모\n\n펭귄 기록".encode()})
    sid = _session(client, alice)
    client.put(f"{API}/smartwork/sessions/{sid}/context", json={"folders": ["work"], "mail": False},
               headers=alice)
    rounds: list = []

    def fake_post_chat(url, headers, payload):
        rounds.append(payload)
        call = lambda i, name, args: {"id": f"c{i}", "function": {  # noqa: E731
            "name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
        if len(rounds) == 1:
            calls = [call(1, "my__read", {"path": "work/plan.md.md"}),
                     call(2, "my__read", {"path": "work/memo.md.md"})]
        elif len(rounds) == 2:
            calls = [call(3, "show_report", {"title": "요약", "content": "<p>근거: plan.md</p>"})]
        else:
            return {"choices": [{"message": {"content": "정리했습니다."}}],
                    "usage": {"prompt_tokens": 3000, "completion_tokens": 10}}
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": calls}}],
                "usage": {"prompt_tokens": 1000 * len(rounds), "completion_tokens": 50}}

    monkeypatch.setattr(llm, "_post_chat", fake_post_chat)
    r = client.post(f"{API}/smartwork/sessions/{sid}/messages", json={"content": "일정 정리해 줘"},
                    headers=alice)
    assert r.status_code == 200, r.text

    with SessionLocal() as db:
        details = db.query(LlmCallDetail).order_by(LlmCallDetail.call_id).all()[-3:]
        turn = db.query(LlmTurn).filter(LlmTurn.turn_id == details[0].turn_id).one()
    assert len({d.turn_id for d in details}) == 1
    first = {(p["part"], p["name"]) for p in details[0].parts}
    assert {("system", "기본 규칙"), ("system", "부서 워크플로"), ("system", "에이전트 목록"),
            ("history", "이번 요청"), ("tools", "")} <= first
    second = {(p["part"], p["name"]) for p in details[1].parts}
    assert ("tool_result", "내 문서") in second  # 출처 — 파일 이름이 아니다
    assert [(p["part"], p["name"]) for p in details[1].output_parts] == [("tool_args", "show_report")]
    assert details[2].output_parts[0]["part"] == "text"

    assert turn.outcome == "보고서"
    assert turn.reads == [{"source": "내 문서", "reads": 2, "cited": 1,
                           "chars": turn.reads[0]["chars"], "uncited_chars": turn.reads[0]["uncited_chars"]}]
    assert 0 < turn.reads[0]["uncited_chars"] < turn.reads[0]["chars"]
    stored = json.dumps([d.parts for d in details] + [turn.reads], ensure_ascii=False)
    assert "plan.md" not in stored and "오로라" not in stored and "일정 정리해 줘" not in stored


def test_the_steps_taken_while_answering_are_shown_and_kept(client, monkeypatch):
    """답을 만드는 동안의 도구 호출이 진행 과정으로 보이고, 답에도 남는다.

    개인 자료 도구는 검색어·파일 이름을 싣지 않는다 — 공유 세션에서는 다른 참여자도 진행 과정을 본다.
    """
    alice = _user(client, "alice@corp.com")
    bob = _user(client, "bob@corp.com")
    client.post(f"{API}/smartwork/personal/consent", headers=alice)
    _upload(client, alice, "work", {"plan.md": "# 계획\n\n오로라".encode()})
    sid = _session(client, alice)
    out, _ = _chat(client, alice, monkeypatch, [
        ("docs__search_docs", {"query": "계약 단가"}),
        ("my__search", {"query": "급여 명세"}),
        ("show_report", {"title": "단가 비교", "content": "<p>x</p>"}),
    ], sid=sid, folders=["work"])
    assert out["message"]["steps"] == ["사내 문서 검색 — 계약 단가", "내 문서·메일 검색", "결과 작성 — 단가 비교"]

    progress = client.get(f"{API}/smartwork/sessions/{sid}/progress", headers=alice).json()
    assert progress["running"] is False and progress["author"] == "alice@corp.com"
    assert [s["label"] for s in progress["steps"]] == out["message"]["steps"]
    assert "급여" not in json.dumps(progress, ensure_ascii=False)
    assert client.get(f"{API}/smartwork/sessions/{sid}/progress", headers=bob).status_code == 404
    # 다시 열어도 그 답의 진행 과정이 남는다
    last = client.get(f"{API}/smartwork/sessions/{sid}", headers=alice).json()["messages"][-1]
    assert last["steps"] == out["message"]["steps"]


# ---------------------------------------------------------------- 대화 문맥(services/convcontext)

class Restate:
    """원장 고치기 대역 — 받은 프롬프트를 남기고 정해 둔 원장을 돌려준다."""

    def __init__(self, answer: dict | str):
        self.answer, self.prompts = answer, []

    def __call__(self, provider, prompt, db):
        self.prompts.append(prompt)
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer, ensure_ascii=False)


TABLE = "<h1>단가 비교</h1><table><tr><th>업체</th><th>단가</th></tr><tr><td>A사</td><td>1,000</td></tr></table>"


def test_later_turns_see_earlier_results_and_the_brief_resolves_what_they_point_to(client, monkeypatch):
    """"그 표에 부서 열 추가" — 다음 턴의 모델은 보고서를 본 적이 없다. 산출물 번호·제목·열을 대화에 남기고
    본문은 session__read로 다시 연다. 원장(요구 통합본)과 풀어 쓴 요청은 이번 요청 바로 앞에 붙는다."""
    alice = _user(client, "alice@corp.com")
    sid = _session(client, alice)
    restate = Restate({"goal": "협력사 단가 비교", "requirements": ["대상: 2026 상반기 계약", "부서 열 추가"],
                       "decisions": [], "open": [],
                       "request": "보고서 「단가 비교」(r0)에 부서 열을 추가해 다시 내 줘", "refers": ["r999"]})
    monkeypatch.setattr(smartwork, "_restate_call", restate)
    first, _ = _chat(client, alice, monkeypatch, [("show_report", {"title": "단가 비교", "content": TABLE})],
                     text="2026 상반기 계약 협력사 단가 비교해 줘", sid=sid)
    assert restate.prompts == []                       # 첫 요청은 풀어 쓸 기록이 없다
    assert "brief" not in first["message"]
    rid = f"r{first['message']['id']}"
    restate.answer["refers"] = [rid, "r999"]           # 목록에 없는 번호는 버린다

    out, script = _chat(client, alice, monkeypatch, [("session__read", {"id": rid})],
                        text="그 표에 부서 열도 넣어 줘", sid=sid)
    # 지난 답에는 낸 것이 번호로 붙는다
    assert f"[결과: {rid} 보고서 「단가 비교」 · 열: 업체, 단가]" in script.messages[-2]["content"]
    last = script.messages[-1]["content"]
    assert last.startswith("[업무 원장") and last.endswith("[이번 요청]\n그 표에 부서 열도 넣어 줘")
    assert "- 부서 열 추가" in last and f"가리키는 것: {rid})" in last
    assert "session__read" in script.tool_names
    assert TABLE in script.results["session__read"]     # 본문은 필요할 때 다시 연다
    # 원장을 고칠 때 본 것: 지난 원장(없음)·산출물·대화·이번 요청
    assert "(없음 — 처음 만든다)" in restate.prompts[0] and f"- {rid} 보고서 「단가 비교」" in restate.prompts[0]
    assert "이번 요청:\n그 표에 부서 열도 넣어 줘" in restate.prompts[0]
    saved = out["message"]
    assert saved["brief"]["requirements"] == ["대상: 2026 상반기 계약", "부서 열 추가"]
    assert saved["restated"] == {"request": restate.answer["request"], "refers": [rid]}

    # 다음 턴은 지난 원장에서부터 고친다 — 그 앞의 대화는 다시 읽지 않는다
    _chat(client, alice, monkeypatch, [], text="금액은 VAT 빼고", sid=sid)
    assert '"부서 열 추가"' in restate.prompts[1]
    assert "2026 상반기 계약 협력사 단가 비교해 줘" not in restate.prompts[1].split("지난 원장 이후의 대화:")[1]


def test_a_brief_that_cannot_be_made_does_not_stop_the_turn(client, monkeypatch):
    """원장 호출이 엉뚱한 글을 주면 원장 없이 답한다 — 원장은 남기지 않는다(다음 턴이 그 사이를 다시 읽는다)."""
    alice = _user(client, "alice@corp.com")
    sid = _session(client, alice)
    monkeypatch.setattr(smartwork, "_restate_call", Restate("원장은 이렇습니다"))
    _chat(client, alice, monkeypatch, [], text="첫 요청", sid=sid)
    out, script = _chat(client, alice, monkeypatch, [], text="그거 다시", sid=sid)
    assert "brief" not in out["message"] and not script.messages[-1]["content"].startswith("[업무 원장")


def test_the_window_is_a_character_budget_and_old_attachments_are_not_resent(client, monkeypatch):
    """창은 메시지 수가 아니라 글자 예산 — 넘친 앞 대화는 원장이 들고 간다. 지난 턴의 첨부 원문은 다시 싣지 않고
    앞부분과 다시 읽을 번호만. 앞을 잘랐으면 여는 지시를 되살리지 않는다(대화 중간에 업무 제안부터 하지 않게)."""
    import base64
    from app.services import convcontext
    alice = _user(client, "alice@corp.com")
    sid = _session(client, alice)
    monkeypatch.setattr(smartwork, "_restate_call", Restate({"goal": "견적 검토", "requirements": ["3천만원 이하"],
                                                            "request": "견적 검토"}))
    long = "가" * 5000
    script = Script([])
    monkeypatch.setattr(llm, "chat_completion", script)
    r = client.post(f"{API}/smartwork/sessions/{sid}/messages", headers=alice, json={
        "content": "이 견적 검토해 줘", "attachments": [
            {"name": "견적.md", "type": "text/markdown",
             "data": base64.b64encode(("# 견적\n금액 3천만원\n" + long).encode()).decode()}]})
    assert r.status_code == 200, r.text
    mid = r.json()["message"]["id"] - 1                # 이 요청(user) 메시지의 번호
    _, script = _chat(client, alice, monkeypatch, [("session__read", {"id": f"a{mid}-0"})], text="금액만", sid=sid)
    earlier = script.messages[1]["content"]
    assert "금액 3천만원" in earlier and f"(전체는 session__read a{mid}-0)" in earlier
    assert len(earlier) < 1000                          # 원문 5천 자는 다시 싣지 않는다
    assert long in script.results["session__read"]

    monkeypatch.setattr(convcontext, "HISTORY_CHARS", 300)
    for i in range(4):
        _, script = _chat(client, alice, monkeypatch, [], text=f"{i}번째 " + "나" * 100, sid=sid)
    roles = [m["role"] for m in script.messages]
    assert roles[0] == "system" and roles[1] == "user"
    assert all(m["content"] != smartwork.OPENING_PROMPT for m in script.messages)
    assert len(script.messages) < 10 and "- 3천만원 이하" in script.messages[-1]["content"]
