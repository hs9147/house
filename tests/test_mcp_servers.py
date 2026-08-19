"""사내 MCP 서버 4종 — 운영 조회·코드 조회·파일 저장소·DB 조회(SELECT 전용)."""
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
        "host_snapshot", "search_audit",
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


def test_code_server_reads_repo(monkeypatch, fresh_settings, tmp_path):
    c, pid = _project_with_repo(monkeypatch, tmp_path)
    path = f"/mcp/projects/{pid}/code"
    names = [t["name"] for t in _rpc(c, path, "tools/list")["result"]["tools"]]
    assert names == ["list_files", "read_file", "read_file_at_ref", "get_code_map"]
    assert _rpc(c, path, "initialize")["result"]["serverInfo"]["name"] == "paas-code-code-mcp"

    files = _text(_call(c, path, "list_files")).split("\n")
    assert set(files) == {"app.py", "README.md"}
    assert _text(_call(c, path, "read_file", {"path": "app.py"})) == "def hello():\n    return 1\n"
    assert "hello" in _text(_call(c, path, "get_code_map"))
    at_ref = _text(_call(c, path, "read_file_at_ref", {"path": "app.py", "ref": "main"}))
    assert "def hello" in at_ref


def test_code_server_rejects_path_escape_and_missing(monkeypatch, fresh_settings, tmp_path):
    c, pid = _project_with_repo(monkeypatch, tmp_path)
    path = f"/mcp/projects/{pid}/code"
    escape = _call(c, path, "read_file", {"path": "../../../etc/passwd"})
    assert escape["error"]["code"] == -32602
    assert _call(c, path, "read_file", {"path": "nope.py"})["error"]["code"] == -32602
    bad_ref = _call(c, path, "read_file_at_ref", {"path": "app.py", "ref": "no-such-branch"})
    assert bad_ref["error"]["code"] == -32602


def test_code_server_unknown_project_is_404(monkeypatch, fresh_settings, tmp_path):
    c, _pid = _project_with_repo(monkeypatch, tmp_path)
    r = c.post(f"{API}/mcp/projects/9999/code", headers=ADMIN,
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 404


# --- 파일 저장소 서버 ---

def _storage_client(monkeypatch, tmp_path, name="assets"):
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    c = _client()
    r = c.post(f"{API}/modules", json={
        "name": name, "type": "file_storage", "config": {"sub_folder": name},
    }, headers=ADMIN)
    assert r.status_code == 201, r.text
    return c


def test_storage_server_write_list_read_delete(monkeypatch, fresh_settings, tmp_path):
    c = _storage_client(monkeypatch, tmp_path)
    path = "/mcp/storage/assets"
    names = [t["name"] for t in _rpc(c, path, "tools/list")["result"]["tools"]]
    assert names == ["list_files", "read_file", "write_file", "delete_file"]

    assert "wrote spec.md" in _text(
        _call(c, path, "write_file", {"path": "spec.md", "content": "# 규격\n"}))
    assert "spec.md" in _text(_call(c, path, "list_files"))
    assert _text(_call(c, path, "read_file", {"path": "spec.md"})) == "# 규격\n"
    assert "deleted spec.md" in _text(_call(c, path, "delete_file", {"path": "spec.md"}))
    assert _call(c, path, "read_file", {"path": "spec.md"})["error"]["code"] == -32602

    # 저장소를 건드린 주체가 감사 로그에 남는다(HTTP 창구와 같은 규칙)
    actions = {row["action"] for row in c.get(f"{API}/audit", headers=ADMIN).json()}
    assert {"mcp.storage.write", "mcp.storage.read", "mcp.storage.delete"} <= actions


def test_storage_server_cannot_escape_module_root(monkeypatch, fresh_settings, tmp_path):
    """이 서버의 존재 이유가 이 가둠이다 — 공개 filesystem MCP는 호스트 디스크를 그대로 연다."""
    c = _storage_client(monkeypatch, tmp_path)
    path = "/mcp/storage/assets"
    for escape in ("../outside.txt", "/etc/passwd"):
        reply = _call(c, path, "write_file", {"path": escape, "content": "x"})
        assert reply["error"]["code"] == -32602, escape
    assert not (tmp_path / "outside.txt").exists()


def test_storage_read_only_module_hides_write_tools(monkeypatch, fresh_settings, tmp_path):
    """사내 문서 공유 폴더처럼 플랫폼이 만들지 않은 디렉터리를 붙일 때 — 목록에 없는
    도구는 모델이 부르지 않고, 불러도 막힌다."""
    monkeypatch.setenv("PAAS_STORAGE_ROOT", str(tmp_path))
    get_settings.cache_clear()
    c = _client()
    r = c.post(f"{API}/modules", json={
        "name": "company-docs", "type": "file_storage",
        "config": {"sub_folder": "docs", "read_only": True},
    }, headers=ADMIN)
    assert r.status_code == 201, r.text

    path = "/mcp/storage/company-docs"
    names = [t["name"] for t in _rpc(c, path, "tools/list")["result"]["tools"]]
    assert names == ["list_files", "read_file"]
    assert _call(c, path, "write_file", {"path": "x.md", "content": "x"})["error"]["code"] == -32601
    assert _call(c, path, "delete_file", {"path": "x.md"})["error"]["code"] == -32601

    # 콘솔 파일 관리 화면(HTTP 창구)에서도 실수로 지워지지 않아야 한다
    up = c.post(f"{API}/storage/company-docs/files", files={"file": ("a.txt", b"x")}, headers=ADMIN)
    assert up.status_code == 403
    rm = c.delete(f"{API}/storage/company-docs/files?path=a.txt", headers=ADMIN)
    assert rm.status_code == 403


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

    everything = json.loads(_text(_call(c, "/mcp/storage/assets", "list_files")))
    assert everything["total"] == 7
    assert everything["truncated"] is False

    # 대소문자 무시 — .PDF와 .pdf가 섞인 폴더가 흔하다
    pdfs = json.loads(_text(_call(c, "/mcp/storage/assets", "list_files", {"glob": "*.pdf"})))
    assert pdfs["total"] == 5

    # 파일명 패턴은 하위 폴더 파일에도 걸린다
    rules = json.loads(_text(_call(c, "/mcp/storage/assets", "list_files", {"glob": "*규정*"})))
    assert [f["path"] for f in rules["files"]] == ["규정/휴가규정.docx"]

    capped = json.loads(
        _text(_call(c, "/mcp/storage/assets", "list_files", {"glob": "*.pdf", "limit": 2})))
    assert len(capped["files"]) == 2
    assert capped["total"] == 5 and capped["truncated"] is True


def test_storage_read_file_extracts_document_text(monkeypatch, fresh_settings, tmp_path):
    """docx를 바이트째로 디코드하면 깨진 글자가 나온다 — 본문을 추출해서 준다."""
    c = _storage_client(monkeypatch, tmp_path)
    root = tmp_path / "assets"
    root.mkdir(parents=True, exist_ok=True)
    from tests.test_doctext import _docx

    _docx(root / "규정.docx", [["매출 정산 규정"], ["분기 마감 후 5일"]])
    text = _text(_call(c, "/mcp/storage/assets", "read_file", {"path": "규정.docx"}))
    assert text == "매출 정산 규정\n분기 마감 후 5일"


def test_storage_read_file_truncates_instead_of_refusing(monkeypatch, fresh_settings, tmp_path):
    """100쪽 PDF를 "너무 큽니다"로 거절하면 읽을 방법이 아예 없어진다."""
    c = _storage_client(monkeypatch, tmp_path)
    root = tmp_path / "assets"
    root.mkdir(parents=True, exist_ok=True)
    from app.api import mcp_servers

    (root / "긴문서.txt").write_text("가" * (mcp_servers._MAX_TEXT_CHARS + 500), encoding="utf-8")
    text = _text(_call(c, "/mcp/storage/assets", "read_file", {"path": "긴문서.txt"}))
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
    reply = _call(c, "/mcp/storage/assets", "read_file", {"path": "old.doc"})
    assert reply["error"]["code"] == -32602
    assert "97-2003" in reply["error"]["message"]


def test_storage_server_rejects_wrong_module_type(monkeypatch, fresh_settings, tmp_path):
    c = _storage_client(monkeypatch, tmp_path)
    c.post(f"{API}/modules", json={
        "name": "paydb", "type": "database", "config": {"dsn": "sqlite:///x.db"},
    }, headers=ADMIN)
    r = c.post(f"{API}/mcp/storage/paydb", headers=ADMIN,
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 400
    r = c.post(f"{API}/mcp/storage/nope", headers=ADMIN,
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 404


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
