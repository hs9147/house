"""사내 MCP 서버 7종 — 운영 조회·코드 조회·문서 검색·파일 저장소·DB 조회(SELECT 전용)·
API 카탈로그·문서 온톨로지(그래프는 tests/test_ontology.py)."""
import subprocess

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.services import deployer

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"


class _FakeRuntime:
    def status(self, name, profile):
        return "running" if profile.value == "release" else "stopped"

    def logs(self, name, profile, tail):
        return f"{name} {profile.value} 로그 {tail}줄"


def _client() -> TestClient:
    return TestClient(create_app())


def _rpc(c: TestClient, path: str, method: str, params: dict | None = None, req_id: int = 1):
    return c.post(f"{API}{path}", headers=ADMIN, json={
        "jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {},
    }).json()


def _call(c: TestClient, path: str, tool: str, args: dict | None = None) -> dict:
    return _rpc(c, path, "tools/call", {"name": tool, "arguments": args or {}})


def _text(reply: dict) -> str:
    return reply["result"]["content"][0]["text"]


# --- 프로토콜 껍데기(services/mcp_server.py) ---

def test_initialize_and_unknown_method_and_tool():
    c = _client()
    init = _rpc(c, "/mcp/ops", "initialize")
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["serverInfo"]["name"] == "paas-ops"
    assert init["result"]["capabilities"] == {"tools": {}}

    assert _rpc(c, "/mcp/ops", "resources/list")["error"]["code"] == -32601
    assert _call(c, "/mcp/ops", "nope")["error"]["code"] == -32601
    # id는 그대로 되돌아온다(JSON-RPC 2.0)
    assert _rpc(c, "/mcp/ops", "tools/list", req_id=77)["id"] == 77


def test_api_key_is_accepted_as_bearer_token():
    """MCP 규약은 자격증명을 Authorization: Bearer로 싣는다(services/mcp_client.py) —
    이 경로가 막히면 플랫폼의 MCP 클라이언트가 플랫폼의 MCP 서버에 붙지 못한다.
    실제로 401이 나던 자리다(x-api-key로만 테스트해서 못 잡았다).
    """
    c = _client()
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    bearer = c.post(f"{API}/mcp/ops", headers={"authorization": "Bearer test-admin-key"}, json=body)
    assert bearer.status_code == 200, bearer.text
    assert bearer.json()["result"]["tools"]

    assert c.post(f"{API}/mcp/ops", headers={"authorization": "Bearer nope"},
                  json=body).status_code == 401
    # JWT 모양(점 2개)은 그대로 OIDC 검증으로 간다 — 그 경로를 가로채지 않는다
    jwt_shaped = c.post(f"{API}/mcp/ops", headers={"authorization": "Bearer aa.bb.cc"}, json=body)
    assert jwt_shaped.status_code == 401
    assert "api key" not in jwt_shaped.json()["detail"]


def test_broken_body_answers_with_jsonrpc_error_not_500():
    c = _client()
    r = c.post(f"{API}/mcp/ops", headers=ADMIN, content=b"not json")
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32601


# --- 운영 조회 서버 ---

def test_ops_tools_list():
    c = _client()
    names = [t["name"] for t in _rpc(c, "/mcp/ops", "tools/list")["result"]["tools"]]
    assert names == [
        "list_routes", "get_deploy_status", "list_deployments", "tail_app_log",
        "host_snapshot", "list_ports", "list_scheduled_jobs", "run_scheduled_job",
        "search_audit",
    ]


def _project(c: TestClient, name="shop-web") -> int:
    r = c.post(f"{API}/projects", json={
        "name": name, "type": "react", "git_url": "https://git.example.com/x",
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_ops_list_routes_matches_server_config_screen(monkeypatch, fresh_settings):
    """화면(/server-config)과 같은 값을 말해야 한다 — 다르면 어느 쪽이 사실인지 알 수 없다."""
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    c = _client()
    _project(c)
    from_tool = _text(_call(c, "/mcp/ops", "list_routes"))
    from_screen = c.get(f"{API}/server-config", headers=ADMIN).json()
    assert '"shop-web"' in from_tool
    assert from_screen["runtime_backend"] in from_tool
    site = next(s for s in from_screen["sites"] if s["profile"] == "release")
    assert site["path_prefix"] in from_tool


def test_ops_get_deploy_status_reports_both_profiles(monkeypatch, fresh_settings):
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    c = _client()
    _project(c)
    import json

    body = json.loads(_text(_call(c, "/mcp/ops", "get_deploy_status", {"project": "shop-web"})))
    assert body["project"] == "shop-web"
    assert body["profiles"]["release"]["status"] == "running"
    assert body["profiles"]["development"]["status"] == "stopped"
    assert body["profiles"]["release"]["path_prefix"] == "/apps/_/shop-web/"
    # 배포 이력이 없으면 None — 없는 값을 만들어 말하지 않는다
    assert body["profiles"]["release"]["last_deployment"] is None
    assert body["profiles"]["release"]["internal_port"] is None


def test_ops_unknown_project_is_tool_error_not_500(monkeypatch, fresh_settings):
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    c = _client()
    reply = _call(c, "/mcp/ops", "get_deploy_status", {"project": "nope"})
    assert reply["error"]["code"] == -32602
    assert "list_routes" in reply["error"]["message"]


def test_ops_tail_app_log_and_bad_profile(monkeypatch, fresh_settings):
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    c = _client()
    _project(c)
    assert "shop-web release 로그 100줄" == _text(
        _call(c, "/mcp/ops", "tail_app_log", {"project": "shop-web"}))
    assert "development 로그 5줄" in _text(
        _call(c, "/mcp/ops", "tail_app_log",
              {"project": "shop-web", "profile": "development", "tail": 5}))
    # LLM이 넣는 상한 초과 값은 서버가 자른다
    assert "로그 500줄" in _text(
        _call(c, "/mcp/ops", "tail_app_log", {"project": "shop-web", "tail": 99999}))
    bad = _call(c, "/mcp/ops", "tail_app_log", {"project": "shop-web", "profile": "prod"})
    assert "unknown profile" in bad["error"]["message"]
    bad_tail = _call(c, "/mcp/ops", "tail_app_log", {"project": "shop-web", "tail": "많이"})
    assert "정수" in bad_tail["error"]["message"]


def test_ops_search_audit_filters(monkeypatch, fresh_settings, tmp_path):
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    c = _client()
    _project(c)  # project.create 감사 이벤트가 남는다
    import json

    rows = json.loads(_text(_call(c, "/mcp/ops", "search_audit", {"action": "project"})))
    assert rows and all("project" in r["action"] for r in rows)
    assert rows[0]["actor"] == "bootstrap-admin"
    assert json.loads(_text(_call(c, "/mcp/ops", "search_audit", {"target": "없는것"}))) == []
    # limit은 상한으로 잘린다(1..50)
    capped = json.loads(_text(_call(c, "/mcp/ops", "search_audit", {"limit": 9999})))
    assert len(capped) <= 50


def test_ops_list_ports_shows_the_registry(monkeypatch, fresh_settings):
    """포트 사용현황은 화면(GET /ports)과 도구가 같은 대장을 읽는다."""
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    from app.db import SessionLocal
    from app.models import BuildProfile
    from app.services import ports

    monkeypatch.setattr(ports, "is_listening", lambda host, port, timeout=0.2: False)
    c = _client()
    pid = _project(c)
    session = SessionLocal()
    try:
        assigned = ports.allocate(session, pid, BuildProfile.release)
    finally:
        session.close()

    import json

    body = json.loads(_text(_call(c, "/mcp/ops", "list_ports")))
    assert body["allocated"] == 1
    assert body["allocations"][0] == {
        "port": assigned, "project": "shop-web", "project_id": pid,
        "profile": "release", "component": None, "listening": False, "in_range": True,
    }
    assert body["range"]["start"] == c.get(f"{API}/ports", headers=ADMIN).json()["range"]["start"]


def test_ops_host_snapshot_returns_runtime_facts(monkeypatch, fresh_settings):
    monkeypatch.setattr(deployer, "get_runtime", lambda: _FakeRuntime())
    c = _client()
    assert "host_os" in _text(_call(c, "/mcp/ops", "host_snapshot"))


# --- 코드 조회 서버 ---

def _project_with_repo(monkeypatch, tmp_path, name="code-mcp"):
    monkeypatch.setenv("PAAS_WORK_DIR", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    repo = tmp_path / "src-repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "app.py").write_text("def hello():\n    return 1\n")
    (repo / "README.md").write_text("# 문서\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "-m", "init"], cwd=repo, check=True)
    c = _client()
    r = c.post(f"{API}/projects", json={
        "name": name, "type": "python", "git_url": str(repo),
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    return c, r.json()["id"]


CODE_PATH = "/mcp/code"


def test_code_server_reads_repo(monkeypatch, fresh_settings, tmp_path):
    """코드 조회는 **서버 하나**다 — 프로젝트는 URL이 아니라 인자로 고른다.

    프로젝트마다 서버를 내주면 프로젝트가 늘어난 만큼 등록할 모듈과 발급할 키가 늘고,
    붙는 쪽도 프로젝트를 바꿀 때마다 다른 서버를 골라야 한다.
    """
    c, _pid = _project_with_repo(monkeypatch, tmp_path)
    path = CODE_PATH
    names = [t["name"] for t in _rpc(c, path, "tools/list")["result"]["tools"]]
    assert names == ["list_projects", "list_files", "read_file",
                     "read_file_at_ref", "get_code_map"]
    assert _rpc(c, path, "initialize")["result"]["serverInfo"]["name"] == "paas-code"

    # 이름을 몰라도 여기서 얻는다
    assert "code-mcp" in _text(_call(c, path, "list_projects")).split("\n")

    proj = {"project": "code-mcp"}
    files = _text(_call(c, path, "list_files", proj)).split("\n")
    assert set(files) == {"app.py", "README.md"}
    assert _text(_call(c, path, "read_file", {**proj, "path": "app.py"})) == \
        "def hello():\n    return 1\n"
    assert "hello" in _text(_call(c, path, "get_code_map", proj))
    at_ref = _text(_call(c, path, "read_file_at_ref",
                         {**proj, "path": "app.py", "ref": "main"}))
    assert "def hello" in at_ref


def test_code_server_rejects_path_escape_and_missing(monkeypatch, fresh_settings, tmp_path):
    c, _pid = _project_with_repo(monkeypatch, tmp_path)
    path, proj = CODE_PATH, {"project": "code-mcp"}
    escape = _call(c, path, "read_file", {**proj, "path": "../../../etc/passwd"})
    assert escape["error"]["code"] == -32602
    assert _call(c, path, "read_file", {**proj, "path": "nope.py"})["error"]["code"] == -32602
    bad_ref = _call(c, path, "read_file_at_ref",
                    {**proj, "path": "app.py", "ref": "no-such-branch"})
    assert bad_ref["error"]["code"] == -32602


def test_code_server_unknown_project_points_at_the_right_discovery_tool(
        monkeypatch, fresh_settings, tmp_path):
    """이름을 틀렸을 때 **이 서버의** 목록 도구를 가리켜야 한다.

    ops 서버는 list_routes로 찾지만 코드 서버에는 그 도구가 없다 — 같은 문구를 돌려쓰면
    있지도 않은 도구를 부르라고 하게 된다.
    """
    c, _pid = _project_with_repo(monkeypatch, tmp_path)
    body = _call(c, CODE_PATH, "list_files", {"project": "없는프로젝트"})
    assert body["error"]["code"] == -32602
    assert "list_projects" in body["error"]["message"]


# --- 파일 저장소 서버 ---

def _storage_client(monkeypatch, tmp_path):
    """플랫폼 저장소 'internal' 하나로 저장소 서버의 기본 동작을 본다.

    이 저장소는 목록에서 숨겨져 있지만 서버는 그대로 산다 — 숨긴 것은 고르는 자리에서
    뺀 것이지 닿지 못하게 한 것이 아니다.
    """
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "assets"))
    monkeypatch.setenv("PAAS_DOC_ROOTS", "")
    get_settings.cache_clear()
    return _client()


def test_storage_server_write_list_read_delete(monkeypatch, fresh_settings, tmp_path):
    c = _storage_client(monkeypatch, tmp_path)
    path = "/mcp/storage/internal"
    names = [t["name"] for t in _rpc(c, path, "tools/list")["result"]["tools"]]
    assert names == ["list_files", "read_file", "search_docs", "reindex_docs",
                     "index_status", "write_file", "delete_file"]

    assert "wrote spec.md" in _text(
        _call(c, path, "write_file", {"path": "spec.md", "content": "# 규격\n"}))
    assert "spec.md" in _text(_call(c, path, "list_files"))
    assert _text(_call(c, path, "read_file", {"path": "spec.md"})) == "# 규격\n"
    # 지우지 않고 휴지통으로 옮긴다 — 어디로 갔는지 함께 말해 준다
    moved = _text(_call(c, path, "delete_file", {"path": "spec.md"}))
    assert "trash" in moved and ".trash/spec.md" in moved
    assert _call(c, path, "read_file", {"path": "spec.md"})["error"]["code"] == -32602

    # 저장소를 건드린 주체가 감사 로그에 남는다(HTTP 창구와 같은 규칙)
    actions = {row["action"] for row in c.get(f"{API}/audit", headers=ADMIN).json()}
    assert {"mcp.storage.write", "mcp.storage.read", "mcp.storage.delete"} <= actions


def test_storage_server_cannot_escape_store_root(monkeypatch, fresh_settings, tmp_path):
    """이 서버의 존재 이유가 이 가둠이다 — 공개 filesystem MCP는 호스트 디스크를 그대로 연다."""
    c = _storage_client(monkeypatch, tmp_path)
    path = "/mcp/storage/internal"
    for escape in ("../outside.txt", "/etc/passwd"):
        reply = _call(c, path, "write_file", {"path": escape, "content": "x"})
        assert reply["error"]["code"] == -32602, escape
    assert not (tmp_path / "outside.txt").exists()


def test_locked_doc_root_hides_write_tools(monkeypatch, fresh_settings, tmp_path):
    """잠근 폴더에서는 쓰기 도구를 아예 광고하지 않는다 — 목록에 없는 도구는 모델이
    부르지 않고, 불러도 막힌다."""
    (tmp_path / "company-docs").mkdir()
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_ROOTS", str(tmp_path / "company-docs"))
    monkeypatch.setenv("PAAS_DOC_ROOTS_READONLY", "company-docs")
    get_settings.cache_clear()
    c = _client()

    path = "/mcp/storage/company-docs"
    names = [t["name"] for t in _rpc(c, path, "tools/list")["result"]["tools"]]
    # 검색·색인은 읽기 동작이라 그대로 남고, 쓰기·삭제만 빠진다
    assert names == ["list_files", "read_file", "search_docs", "reindex_docs",
                     "index_status"]
    assert _call(c, path, "write_file", {"path": "x.md", "content": "x"})["error"]["code"] == -32601
    assert _call(c, path, "delete_file", {"path": "x.md"})["error"]["code"] == -32601

    # 콘솔 파일 관리 화면(HTTP 창구)에서도 실수로 지워지지 않아야 한다
    up = c.post(f"{API}/storage/company-docs/files", files={"file": ("a.txt", b"x")}, headers=ADMIN)
    assert up.status_code == 403
    rm = c.delete(f"{API}/storage/company-docs/files?path=a.txt", headers=ADMIN)
    assert rm.status_code == 403


def test_unlocked_doc_root_advertises_write_tools(monkeypatch, fresh_settings, tmp_path):
    """잠그지 않은 폴더의 서버에만 쓰기 도구가 뜬다.

    **이 성질은 폴더가 URL에 있어서 성립한다.** 저장소 서버를 하나로 합쳐 폴더를 인자로
    받게 하면 write_file·delete_file이 항상 목록에 뜨고, 계약 폴더를 다루는 문맥에서도
    모델이 쓰기 도구를 보게 된다. 읽기만 필요한 쪽은 /mcp/docs 하나로 전 폴더를 가로지르면
    되므로, 저장소 서버를 폴더별로 두는 대가는 크지 않다.
    """
    (tmp_path / "scratch").mkdir()
    (tmp_path / "contract").mkdir()
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_ROOTS",
                       f"scratch={tmp_path / 'scratch'},contract={tmp_path / 'contract'}")
    monkeypatch.setenv("PAAS_DOC_ROOTS_READONLY", "contract")
    get_settings.cache_clear()
    c = _client()

    opened = [t["name"] for t in _rpc(c, "/mcp/storage/scratch", "tools/list")["result"]["tools"]]
    assert "write_file" in opened and "delete_file" in opened
    _call(c, "/mcp/storage/scratch", "write_file", {"path": "memo.md", "content": "# 메모\n"})
    assert (tmp_path / "scratch" / "memo.md").read_text(encoding="utf-8") == "# 메모\n"

    # 같은 설정의 다른 폴더에는 그 도구가 **보이지도 않는다**
    locked = [t["name"] for t in _rpc(c, "/mcp/storage/contract", "tools/list")["result"]["tools"]]
    assert "write_file" not in locked and "delete_file" not in locked
    assert _call(c, "/mcp/storage/contract", "write_file",
                 {"path": "x.md", "content": "x"})["error"]["code"] == -32601


def test_storage_list_files_filters_and_caps(monkeypatch, fresh_settings, tmp_path):
    """문서 폴더는 파일이 수천 개다 — 전체 목록을 그대로 내주면 컨텍스트가 통째로 찬다."""
    c = _storage_client(monkeypatch, tmp_path)
    root = tmp_path / "assets"
    (root / "규정").mkdir(parents=True)
    for i in range(5):
        (root / f"보고서{i}.PDF").write_bytes(b"x")
    (root / "규정" / "휴가규정.docx").write_bytes(b"x")
    (root / "메모.txt").write_bytes(b"x")

    import json

    everything = json.loads(_text(_call(c, "/mcp/storage/internal", "list_files")))
    assert everything["total"] == 7
    assert everything["truncated"] is False

    # 대소문자 무시 — .PDF와 .pdf가 섞인 폴더가 흔하다
    pdfs = json.loads(_text(_call(c, "/mcp/storage/internal", "list_files", {"glob": "*.pdf"})))
    assert pdfs["total"] == 5

    # 파일명 패턴은 하위 폴더 파일에도 걸린다
    rules = json.loads(_text(_call(c, "/mcp/storage/internal", "list_files", {"glob": "*규정*"})))
    assert [f["path"] for f in rules["files"]] == ["규정/휴가규정.docx"]

    capped = json.loads(
        _text(_call(c, "/mcp/storage/internal", "list_files", {"glob": "*.pdf", "limit": 2})))
    assert len(capped["files"]) == 2
    assert capped["total"] == 5 and capped["truncated"] is True


def test_storage_read_file_extracts_document_text(monkeypatch, fresh_settings, tmp_path):
    """docx를 바이트째로 디코드하면 깨진 글자가 나온다 — 본문을 추출해서 준다."""
    c = _storage_client(monkeypatch, tmp_path)
    root = tmp_path / "assets"
    root.mkdir(parents=True, exist_ok=True)
    from tests.test_doctext import _docx

    _docx(root / "규정.docx", [["매출 정산 규정"], ["분기 마감 후 5일"]])
    text = _text(_call(c, "/mcp/storage/internal", "read_file", {"path": "규정.docx"}))
    # 마크다운이므로 단락 사이가 빈 줄이다 — 줄바꿈 하나는 같은 문단의 이어짐을 뜻한다
    assert text == "매출 정산 규정\n\n분기 마감 후 5일"


def test_storage_read_file_truncates_instead_of_refusing(monkeypatch, fresh_settings, tmp_path):
    """100쪽 PDF를 "너무 큽니다"로 거절하면 읽을 방법이 아예 없어진다."""
    c = _storage_client(monkeypatch, tmp_path)
    root = tmp_path / "assets"
    root.mkdir(parents=True, exist_ok=True)
    from app.api import mcp_servers

    (root / "긴문서.txt").write_text("가" * (mcp_servers._MAX_TEXT_CHARS + 500), encoding="utf-8")
    text = _text(_call(c, "/mcp/storage/internal", "read_file", {"path": "긴문서.txt"}))
    assert "만 표시" in text
    assert str(mcp_servers._MAX_TEXT_CHARS + 500) in text


def test_storage_read_file_reports_unreadable_format(monkeypatch, fresh_settings, tmp_path):
    """추출 불가는 깨진 텍스트가 아니라 이유로 돌려준다."""
    c = _storage_client(monkeypatch, tmp_path)
    root = tmp_path / "assets"
    root.mkdir(parents=True, exist_ok=True)
    (root / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 32)
    from app.services import doctext

    monkeypatch.setattr(doctext, "_soffice", lambda: None)
    reply = _call(c, "/mcp/storage/internal", "read_file", {"path": "old.doc"})
    assert reply["error"]["code"] == -32602
    assert "97-2003" in reply["error"]["message"]


def test_storage_search_docs_flow(monkeypatch, fresh_settings, tmp_path):
    """색인 → 검색 → 커버리지. 파일명이 아니라 본문으로 찾는 것이 요점이다."""
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    c = _storage_client(monkeypatch, tmp_path)
    root = tmp_path / "assets"
    root.mkdir(parents=True, exist_ok=True)
    from tests.test_doctext import _docx

    _docx(root / "A-2025-11.docx", [["반출 승인 절차"], ["담당: 총무팀"]])
    (root / "메모.txt").write_text("휴가규정 개정", encoding="utf-8")
    path = "/mcp/storage/internal"

    import json

    # 색인 전 검색 — "못 찾음"과 "색인이 없음"은 다른 문제다
    empty = _call(c, path, "search_docs", {"query": "반출"})
    assert "색인이 비어" in empty["error"]["message"]

    built = json.loads(_text(_call(c, path, "reindex_docs")))
    assert built["indexed"] == 2 and built["done"] is True

    found = json.loads(_text(_call(c, path, "search_docs", {"query": "반출 승인"})))
    assert [h["path"] for h in found["hits"]] == ["A-2025-11.docx"]
    assert "반출 승인 절차" in found["hits"][0]["snippets"][0]

    # 파일명으로는 알 수 없는 것을 찾는다
    assert [h["path"] for h in json.loads(
        _text(_call(c, path, "search_docs", {"query": "총무팀"})))["hits"]] == ["A-2025-11.docx"]

    # 찾지 못한 경우에는 색인 상태를 함께 알려 준다(질의 문제인지 색인 문제인지 갈리게)
    miss = json.loads(_text(_call(c, path, "search_docs", {"query": "없는말"})))
    assert miss["hits"] == [] and miss["index"] == {"indexed": 2, "failed": 0}

    report = json.loads(_text(_call(c, path, "index_status")))
    assert report["total"] == 2 and report["by_suffix"][".docx"]["indexed"] == 1

    actions = {row["action"] for row in c.get(f"{API}/audit", headers=ADMIN).json()}
    assert {"mcp.docs.reindex", "mcp.docs.search"} <= actions


def test_storage_server_rejects_unknown_store(monkeypatch, fresh_settings, tmp_path):
    c = _storage_client(monkeypatch, tmp_path)
    r = c.post(f"{API}/mcp/storage/nope", headers=ADMIN,
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 404


def test_storage_server_reports_broken_doc_roots(monkeypatch, fresh_settings, tmp_path):
    """환경변수가 잘못된 것은 요청 잘못이 아니다 — 어느 항목이 문제인지 그대로 말한다."""
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("PAAS_DOC_ROOTS", "=/srv/docs")
    get_settings.cache_clear()
    r = _client().post(f"{API}/mcp/storage/internal", headers=ADMIN,
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 500
    assert "'=/srv/docs'" in r.json()["detail"]


# --- DB 조회 서버 ---

def _db_client(monkeypatch, tmp_path, allow: str = "paydb"):
    sqlite_path = tmp_path / "pay.db"
    import sqlite3

    conn = sqlite3.connect(sqlite_path)
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, amount INTEGER NOT NULL)")
    conn.executemany("INSERT INTO orders (amount) VALUES (?)", [(i,) for i in range(1, 6)])
    conn.commit()
    conn.close()

    monkeypatch.setenv("PAAS_MCP_DB_MODULES", allow)
    get_settings.cache_clear()
    c = _client()
    r = c.post(f"{API}/modules", json={
        "name": "paydb", "type": "database", "config": {"dsn": f"sqlite:///{sqlite_path}"},
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    return c


def test_db_server_blocked_unless_module_is_allowlisted(monkeypatch, fresh_settings, tmp_path):
    """기본값은 빈 목록 = 전부 차단. 등록만으로 LLM에게 열리지 않는다."""
    c = _db_client(monkeypatch, tmp_path, allow="")
    r = c.post(f"{API}/mcp/db/paydb", headers=ADMIN,
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 403
    assert "PAAS_MCP_DB_MODULES" in r.json()["detail"]


def test_db_server_lists_describes_and_selects(monkeypatch, fresh_settings, tmp_path):
    c = _db_client(monkeypatch, tmp_path)
    path = "/mcp/db/paydb"
    names = [t["name"] for t in _rpc(c, path, "tools/list")["result"]["tools"]]
    assert names == ["list_tables", "describe_table", "run_select"]

    assert "orders" in _text(_call(c, path, "list_tables"))
    described = _text(_call(c, path, "describe_table", {"table": "orders"}))
    assert "amount" in described and '"primary_key"' in described
    assert _call(c, path, "describe_table", {"table": "nope"})["error"]["code"] == -32602

    import json

    body = json.loads(_text(_call(c, path, "run_select",
                                  {"sql": "SELECT amount FROM orders ORDER BY id"})))
    assert body["columns"] == ["amount"]
    assert [r["amount"] for r in body["rows"]] == [1, 2, 3, 4, 5]
    assert body["truncated"] is False

    # 실행한 SQL은 감사 로그에 남는다
    audit = c.get(f"{API}/audit", headers=ADMIN).json()
    entry = next(row for row in audit if row["action"] == "mcp.db.select")
    assert "SELECT amount FROM orders" in entry["detail"]["sql"]
    assert entry["detail"]["rows"] == 5


def test_db_server_caps_rows(monkeypatch, fresh_settings, tmp_path):
    c = _db_client(monkeypatch, tmp_path)
    import json

    body = json.loads(_text(_call(c, "/mcp/db/paydb", "run_select",
                                  {"sql": "SELECT * FROM orders", "limit": 2})))
    assert body["row_count"] == 2
    assert body["truncated"] is True


def test_db_server_rejects_writes_and_multiple_statements(monkeypatch, fresh_settings, tmp_path):
    c = _db_client(monkeypatch, tmp_path)
    path = "/mcp/db/paydb"
    rejected = [
        "DELETE FROM orders",
        "UPDATE orders SET amount = 0",
        "DROP TABLE orders",
        "SELECT 1; DELETE FROM orders",
        "SELECT * FROM orders -- \n; DROP TABLE orders",
        "SELECT * INTO backup FROM orders",
        "INSERT INTO orders (amount) VALUES (9)",
    ]
    for sql in rejected:
        reply = _call(c, path, "run_select", {"sql": sql})
        assert "error" in reply, sql

    # 실제로 아무 것도 바뀌지 않았다
    import json

    body = json.loads(_text(_call(c, path, "run_select", {"sql": "SELECT count(*) c FROM orders"})))
    assert body["rows"][0]["c"] == 5


def test_db_server_sql_error_comes_back_as_tool_error(monkeypatch, fresh_settings, tmp_path):
    """모델이 고쳐 다시 시도할 수 있게 500이 아니라 도구 오류로 돌려준다."""
    c = _db_client(monkeypatch, tmp_path)
    reply = _call(c, "/mcp/db/paydb", "run_select", {"sql": "SELECT nope FROM orders"})
    assert reply["error"]["code"] == -32602
    assert "실행 실패" in reply["error"]["message"]


def test_db_server_reports_missing_driver(monkeypatch, fresh_settings, tmp_path):
    """드라이버는 선택 의존성이다 — 무엇을 설치해야 하는지 말해 준다."""
    c = _db_client(monkeypatch, tmp_path)
    from app.api import mcp_servers

    def boom(*a, **kw):
        raise ModuleNotFoundError("No module named 'psycopg2'")

    monkeypatch.setattr("sqlalchemy.create_engine", boom)
    reply = _call(c, "/mcp/db/paydb", "list_tables")
    assert "드라이버가 설치되지 않았습니다" in reply["error"]["message"]
    assert mcp_servers is not None


# --- 사내 문서 검색 서버 (/mcp/docs) ---

def _docs_client(monkeypatch, tmp_path, sources=("company-docs",)):
    """문서 폴더 여러 개에 문서를 흩어 놓는다 — 가로질러 찾는 것이 이 서버의 이유다.

    내부 저장소(PAAS_STORAGE_ROOT)도 검색 대상이라 문서 폴더 밖에 따로 둔다 — 안에
    두면 같은 파일이 두 저장소에 걸쳐 두 번 색인된다.
    """
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    for name in sources:
        (tmp_path / "shared" / name).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAAS_DOC_ROOTS",
                       ",".join(str(tmp_path / "shared" / n) for n in sources))
    get_settings.cache_clear()
    return _client()


def test_docs_server_searches_across_every_source(monkeypatch, fresh_settings, tmp_path):
    c = _docs_client(monkeypatch, tmp_path, ("hr-docs", "fin-docs"))
    from tests.test_doctext import _docx

    _docx(tmp_path / "shared" / "hr-docs" / "휴가규정.docx", [["휴가 규정"], ["담당: 총무팀"]])
    (tmp_path / "shared" / "fin-docs" / "정산.txt").write_text("매출 정산 규정 총무팀 확인", encoding="utf-8")

    import json

    names = [t["name"] for t in _rpc(c, "/mcp/docs", "tools/list")["result"]["tools"]]
    assert names == ["list_sources", "search_docs", "read_doc", "reindex_docs", "index_status"]
    assert _rpc(c, "/mcp/docs", "initialize")["result"]["serverInfo"]["name"] == "paas-docs"

    built = json.loads(_text(_call(c, "/mcp/docs", "reindex_docs")))
    assert built["done"] is True
    assert set(built["sources"]) == {"internal", "hr-docs", "fin-docs"}

    hits = json.loads(_text(_call(c, "/mcp/docs", "search_docs", {"query": "총무팀"})))["hits"]
    # 어느 저장소인지가 결과에 실려 온다 — 그래야 read_doc을 부를 수 있다
    assert {(h["source"], h["path"]) for h in hits} == {
        ("hr-docs", "휴가규정.docx"), ("fin-docs", "정산.txt")}

    only = json.loads(_text(_call(c, "/mcp/docs", "search_docs",
                                  {"query": "규정", "source": "fin-docs"})))
    assert [h["source"] for h in only["hits"]] == ["fin-docs"]
    assert only["searched"] == ["fin-docs"]


def test_docs_server_reads_a_hit(monkeypatch, fresh_settings, tmp_path):
    c = _docs_client(monkeypatch, tmp_path)
    from tests.test_doctext import _docx

    _docx(tmp_path / "shared" / "company-docs" / "규정.docx", [["반출 승인 절차"], ["담당: 총무팀"]])
    _call(c, "/mcp/docs", "reindex_docs")
    text = _text(_call(c, "/mcp/docs", "read_doc",
                       {"source": "company-docs", "path": "규정.docx"}))
    assert text == "반출 승인 절차\n\n담당: 총무팀"

    escaped = _call(c, "/mcp/docs", "read_doc",
                    {"source": "company-docs", "path": "../밖.txt"})
    assert escaped["error"]["code"] == -32602


def test_docs_server_distinguishes_empty_index_from_no_match(monkeypatch, fresh_settings, tmp_path):
    c = _docs_client(monkeypatch, tmp_path)
    (tmp_path / "shared" / "company-docs" / "메모.txt").write_text("휴가 규정", encoding="utf-8")

    empty = _call(c, "/mcp/docs", "search_docs", {"query": "휴가"})
    assert "색인이 비어" in empty["error"]["message"]

    _call(c, "/mcp/docs", "reindex_docs")
    import json

    miss = json.loads(_text(_call(c, "/mcp/docs", "search_docs", {"query": "없는말"})))
    assert miss["hits"] == [] and miss["index"]["company-docs"] == {"indexed": 1, "failed": 0}


def test_docs_server_points_at_the_env_var_when_nothing_is_indexed(
        monkeypatch, fresh_settings, tmp_path):
    """색인이 비었을 때 원인은 둘이다 — 아직 안 돌렸거나, 폴더 설정이 틀렸거나."""
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path / "internal"))
    monkeypatch.setenv("PAAS_DOC_INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setenv("PAAS_DOC_ROOTS", "")
    get_settings.cache_clear()
    reply = _call(_client(), "/mcp/docs", "search_docs", {"query": "규정"})
    assert "reindex_docs" in reply["error"]["message"]
    assert "PAAS_DOC_ROOTS" in reply["error"]["message"]


def test_docs_server_lists_sources_with_coverage(monkeypatch, fresh_settings, tmp_path):
    c = _docs_client(monkeypatch, tmp_path)
    (tmp_path / "shared" / "company-docs" / "메모.txt").write_text("휴가 규정", encoding="utf-8")
    (tmp_path / "shared" / "company-docs" / "구버전.doc").write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 32)
    from app.services import doctext

    monkeypatch.setattr(doctext, "_soffice", lambda: None)
    _call(c, "/mcp/docs", "reindex_docs")

    import json

    sources = {row["source"]: row for row in
               json.loads(_text(_call(c, "/mcp/docs", "list_sources")))}
    assert sources["company-docs"] == {
        "source": "company-docs", "root": str(tmp_path / "shared" / "company-docs"),
        "exists": True, "read_only": False,
        "index": {"total": 2, "indexed": 1, "failed": 1}}
    # 숨긴 저장소도 여기서는 나온다 — /mcp/docs는 "전체에서 찾아라"는 창구다
    assert sources["internal"]["read_only"] is False
    status = json.loads(_text(_call(c, "/mcp/docs", "index_status")))
    assert "97-2003" in " ".join(status["company-docs"]["failure_reasons"])


def test_docs_server_appears_in_the_internal_directory(monkeypatch, fresh_settings, tmp_path):
    monkeypatch.setenv("PAAS_MCP_INTERNAL_BASE_URL", "http://localhost:7000/paas")
    c = _docs_client(monkeypatch, tmp_path)
    items = {i["id"]: i for i in c.get(f"{API}/mcp/search", headers=ADMIN).json()}
    assert items["paas-docs"]["url"] == "http://localhost:7000/paas/api/v1/mcp/docs"
    # 저장소마다 전용 서버도 함께 올라간다
    assert items["paas-storage-company-docs"]["url"].endswith("/mcp/storage/company-docs")


# --- 외부 API 카탈로그 서버 (/mcp/apis) ---

_FAKE_CATALOG = {"stripe.com": {"preferred": "1.0", "versions": {"1.0": {
    "info": {"title": "Stripe", "description": "Online payment processing",
             "x-apisguru-categories": ["financial"]},
    "swaggerUrl": "https://example.test/swagger.json"}}}}


def _apis_client(monkeypatch) -> TestClient:
    """카탈로그를 한 번 수집해 둔 클라이언트.

    수집이 끝난 뒤 httpx를 폭탄으로 바꾼다 — 이 서버가 실제로 밖으로 나가지 않는다는
    것이 테스트마다 성립해야 하는 성질이라서다(그것이 admin 없이 여는 근거다).
    """
    from app.services import httpx_retry

    httpx_retry.reset_breakers()
    monkeypatch.setenv("PAAS_PUBLIC_DATA_URL", "")
    get_settings.cache_clear()

    class _R:
        status_code = 200

        def json(self):
            return _FAKE_CATALOG

    monkeypatch.setattr(httpx_retry.httpx, "get", lambda url, **kw: _R())
    c = _client()
    assert c.post(f"{API}/modules/search/refresh", headers=ADMIN).json()["added"] == 1

    def boom(url, **kw):
        raise AssertionError(f"카탈로그 서버가 밖으로 나갔다: {url}")

    monkeypatch.setattr(httpx_retry.httpx, "get", boom)
    return c


def test_apis_server_searches_the_collected_catalog(monkeypatch, fresh_settings):
    import json

    c = _apis_client(monkeypatch)
    tools = {t["name"] for t in _rpc(c, "/mcp/apis", "tools/list")["result"]["tools"]}
    assert tools == {"search_apis", "list_api_categories", "catalog_status", "sync_catalog"}

    hits = json.loads(_text(_call(c, "/mcp/apis", "search_apis", {"keyword": "payment"})))
    assert [h["id"] for h in hits] == ["stripe.com"]
    assert json.loads(_text(_call(c, "/mcp/apis", "list_api_categories"))) == [
        {"name": "financial", "count": 1}]
    assert json.loads(_text(_call(c, "/mcp/apis", "catalog_status")))["total"] == 1


def test_apis_server_sync_is_throttled_and_audited(monkeypatch, fresh_settings):
    """모델이 부르는 수집이다 — 넓히는 것은 권한이 아니라 호출 횟수뿐이므로 거기를 막는다.

    _apis_client는 수집을 한 번 끝낸 뒤 httpx를 폭탄으로 바꿔 둔다. 최소 간격이 걸려
    있으면 두 번째 호출은 밖으로 나가지 않고 skipped로 돌아온다 — 폭탄이 안 터지는
    것이 곧 "안 나갔다"의 증거다.
    """
    import json

    from app.services import apisearch

    c = _apis_client(monkeypatch)
    body = json.loads(_text(_call(c, "/mcp/apis", "sync_catalog")))
    assert body["skipped"] == [apisearch.SOURCE_APISGURU]
    assert body["sources"] == []

    # 밖으로 나가는 유일한 도구라 감사에 남는다
    actions = {row["action"] for row in c.get(f"{API}/audit", headers=ADMIN).json()}
    assert "mcp.apis.sync" in actions

    # 켜져 있지 않은 소스를 콕 집으면 조용한 빈 결과가 아니라 오류다
    bad = _call(c, "/mcp/apis", "sync_catalog",
                {"source": apisearch.SOURCE_PUBLIC_DATA})
    assert "켜져 있지 않은" in bad["error"]["message"]


def test_apis_server_works_without_an_admin_key(monkeypatch, fresh_settings):
    """mcp 모듈의 api_key는 배포된 앱의 환경변수로도 주입된다 — 관리자 키를 붙이게
    만들면 안 되므로, 이 서버는 관리자 아닌 키로 동작해야 한다."""
    c = _apis_client(monkeypatch)
    member = c.post(f"{API}/keys", json={"name": "app"}, headers=ADMIN).json()["key"]
    r = c.post(f"{API}/mcp/apis", headers={"x-api-key": member}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_apis", "arguments": {"keyword": "payment"}},
    })
    assert r.status_code == 200
    assert "stripe.com" in r.json()["result"]["content"][0]["text"]
    # 같은 검색의 HTTP 창구는 모듈 등록으로 이어지므로 그대로 admin 전용이다
    assert c.get(f"{API}/modules/search", params={"keyword": "payment"},
                 headers={"x-api-key": member}).status_code == 403


def test_apis_server_answers_no_match_with_an_empty_list(monkeypatch, fresh_settings):
    c = _apis_client(monkeypatch)
    assert _text(_call(c, "/mcp/apis", "search_apis", {"keyword": "없는말"})) == "[]"


def test_apis_server_says_the_catalog_was_never_collected():
    """수집한 적 없는 것을 빈 목록으로 돌려주면 모델은 질의를 바꿔 가며 헛돌게 된다."""
    reply = _call(_client(), "/mcp/apis", "search_apis", {"keyword": "payment"})
    assert "수집하지 않았습니다" in reply["error"]["message"]


def test_apis_server_appears_in_the_internal_directory(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_MCP_INTERNAL_BASE_URL", "http://localhost:7000/paas")
    get_settings.cache_clear()
    items = {i["id"]: i for i in _client().get(f"{API}/mcp/search", headers=ADMIN).json()}
    assert items["paas-apis"]["url"] == "http://localhost:7000/paas/api/v1/mcp/apis"


def test_apis_server_can_narrow_to_one_source(monkeypatch, fresh_settings):
    """LLM도 "국내 공공데이터만"이라고 좁힐 수 있어야 한다."""
    import json

    from app.services import apisearch

    c = _apis_client(monkeypatch)
    guru = json.loads(_text(_call(c, "/mcp/apis", "search_apis",
                                  {"source": apisearch.SOURCE_APISGURU})))
    assert [h["id"] for h in guru] == ["stripe.com"]
    # 수집한 적 없는 소스는 빈 목록이다(오류가 아니다 — 조건이 맞는 항목이 없는 것이다)
    assert _text(_call(c, "/mcp/apis", "search_apis",
                       {"source": apisearch.SOURCE_PUBLIC_DATA})) == "[]"

    # 모르는 소스는 빈 목록이 아니라 오류다
    bad = _call(c, "/mcp/apis", "search_apis", {"source": "공공데이터"})
    assert "모르는 소스" in bad["error"]["message"]

    # 현황에는 두 소스가 다 나온다 — 왜 비었는지 모델이 여기서 확인한다
    status = json.loads(_text(_call(c, "/mcp/apis", "catalog_status")))
    assert status["sources"]["publicdata"]["enabled"] is False
    assert status["sources"]["publicdata"]["total"] == 0


def test_apis_server_finds_an_api_by_its_url(monkeypatch, fresh_settings):
    """받아 둔 주소가 무슨 API였는지 되짚는 것도 검색이다."""
    import json

    c = _apis_client(monkeypatch)
    hits = json.loads(_text(_call(c, "/mcp/apis", "search_apis",
                                  {"keyword": "https://example.test/swagger.json"})))
    assert [h["id"] for h in hits] == ["stripe.com"]


def test_apis_server_sync_refreshes_on_request(monkeypatch, fresh_settings):
    """요청이 있을 때 최신화한다 — 도구가 실제로 나가서 표를 갱신하는지 본다."""
    import json

    from app.services import apisearch, httpx_retry

    httpx_retry.reset_breakers()
    monkeypatch.setenv("PAAS_PUBLIC_DATA_URL", "")
    get_settings.cache_clear()
    catalog = {"stripe.com": {"preferred": "1.0", "versions": {"1.0": {
        "info": {"title": "Stripe", "description": "결제"},
        "swaggerUrl": "https://example.test/s.json"}}}}

    class _R:
        status_code = 200

        def json(self):
            return catalog

    monkeypatch.setattr(httpx_retry.httpx, "get", lambda url, **kw: _R())
    c = _client()

    first = json.loads(_text(_call(c, "/mcp/apis", "sync_catalog")))
    assert (first["added"], first["skipped"]) == (1, [])
    assert json.loads(_text(_call(c, "/mcp/apis", "search_apis",
                                  {"keyword": "결제"})))[0]["id"] == "stripe.com"

    # 최소 간격 안에 다시 부르면 나가지 않는다 — 오류가 아니라 "방금 받았다"
    again = json.loads(_text(_call(c, "/mcp/apis", "sync_catalog")))
    assert again["skipped"] == [apisearch.SOURCE_APISGURU]
    assert again["added"] == 0

    # 간격을 비우면 다시 나간다. 이번에는 바뀐 것이 없으므로 unchanged다
    apisearch.reset_sync_throttle()
    third = json.loads(_text(_call(c, "/mcp/apis", "sync_catalog")))
    assert (third["added"], third["updated"], third["unchanged"]) == (0, 0, 1)
