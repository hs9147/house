"""공용 DB — 앱마다 DB·계정을 따로 만들고, 공용 자료는 읽기만, 지운 프로젝트의 DB는 남긴다.

PostgreSQL이 없는 곳에서 돈다: DB에 닿는 유일한 자리(shareddb._execute)를 가짜 서버로 바꾸고,
무엇을 어떤 순서로 만들었는지(나간 SQL)를 본다. 실제 서버에서의 동작은 infra/postgres/README.md의
점검 절차로 확인한다.
"""
import re
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import (
    BuildProfile, Module, ModuleBinding, Project, ScheduledJob, SharedDatabase,
)
from app.services import deployer, modules, planning, scheduler, shareddb

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"
ADMIN_URL = "postgresql+psycopg://paas_admin:adminpw@127.0.0.1:5432/postgres"


class FakeServer:
    """계정·DB가 있는지만 기억하고, 나간 SQL을 남긴다."""

    def __init__(self):
        self.roles: set[str] = set()
        self.dbs: set[str] = set()
        self.log: list[tuple[str | None, str]] = []
        self.columns: dict[str, list[tuple]] = {}

    def __call__(self, dbname, sql):
        self.log.append((dbname, sql))
        if m := re.match(r"SELECT 1 FROM pg_roles WHERE rolname = '(\w+)'", sql):
            return [(1,)] if m.group(1) in self.roles else []
        if m := re.match(r"SELECT 1 FROM pg_database WHERE datname = '(\w+)'", sql):
            return [(1,)] if m.group(1) in self.dbs else []
        if m := re.match(r'CREATE ROLE "(\w+)"', sql):
            self.roles.add(m.group(1))
        if m := re.match(r'CREATE DATABASE "(\w+)"', sql):
            self.dbs.add(m.group(1))
        if sql.startswith("SELECT table_name"):
            return self.columns.get(dbname, [])
        return []

    def sql(self, dbname=None) -> list[str]:
        return [s for d, s in self.log if d == dbname]


@pytest.fixture
def server(monkeypatch, fresh_settings):
    monkeypatch.setenv("PAAS_SHARED_DB_ADMIN_URL", ADMIN_URL)
    get_settings.cache_clear()
    fake = FakeServer()
    monkeypatch.setattr(shareddb, "_execute", fake)
    create_app()  # 표(create_all)와 「공용 DB」 모듈
    return fake


def _project(name="supplier-pool") -> int:
    with SessionLocal() as db:
        project = Project(name=name, type="react", git_url=f"https://git.test/{name}")
        db.add(project)
        db.flush()
        module = db.query(Module).filter(Module.name == shareddb.MODULE_NAME).one()
        db.add(ModuleBinding(project_id=project.id, module_id=module.id, env_prefix="DB"))
        db.commit()
        return project.id


def test_each_app_gets_its_own_database_per_profile(server):
    """바인딩된 앱은 배포할 때 자기 DB를 받는다 — 운영과 개발도 따로, 다른 계정은 접속 못 한다."""
    pid = _project()
    with SessionLocal() as db:
        project = db.get(Project, pid)
        release = deployer.resolve_env(db, project, BuildProfile.release)["DB_DSN"]
        dev = deployer.resolve_env(db, project, BuildProfile.development)["DB_DSN"]
        rows = db.query(SharedDatabase).filter(SharedDatabase.project_id == pid).all()

    assert re.fullmatch(r"postgresql://app_supplier_pool:[\w-]+@127\.0\.0\.1:5432/app_supplier_pool", release)
    assert dev.endswith("/app_supplier_pool_dev") and dev.split(":")[2] != release.split(":")[2]
    admin = server.sql()
    assert 'CREATE DATABASE "app_supplier_pool" OWNER "app_supplier_pool"' in admin
    assert 'REVOKE CONNECT ON DATABASE "app_supplier_pool" FROM PUBLIC' in admin
    assert 'GRANT "common_reader" TO "app_supplier_pool"' in admin
    # 비밀번호는 암호화해 두고, DB 행 어디에도 평문이 없다
    password = release.split(":")[2].split("@")[0]
    assert all(password not in r.password_encrypted for r in rows)


def test_provisioning_is_idempotent(server):
    """배포마다 부른다 — 이미 있으면 다시 만들지 않는다(비밀번호만 우리 것으로 맞춘다)."""
    pid = _project()
    with SessionLocal() as db:
        project = db.get(Project, pid)
        first = shareddb.dsn(shareddb.provision(db, project, BuildProfile.release))
        before = len([s for s in server.sql() if s.startswith("CREATE")])
        second = shareddb.dsn(shareddb.provision(db, project, BuildProfile.release))
    assert first == second
    assert len([s for s in server.sql() if s.startswith("CREATE")]) == before
    assert any(s.startswith('ALTER ROLE "app_supplier_pool" LOGIN PASSWORD') for s in server.sql())


def test_common_data_is_read_only_unless_the_admin_allows_one_app(server):
    """공용 자료 DB는 모든 앱이 읽기만 — 쓰기는 관리자가 맡긴 앱의 **운영** 계정에만."""
    pid = _project()
    with SessionLocal() as db:
        project = db.get(Project, pid)
        with pytest.raises(shareddb.SharedDbError, match="운영 DB가 없습니다"):
            shareddb.set_common_writer(db, project, True)
        shareddb.provision(db, project, BuildProfile.release)
        shareddb.provision(db, project, BuildProfile.development)
        assert 'GRANT CREATE ON SCHEMA public TO "app_supplier_pool"' not in server.sql(shareddb.COMMON_DB)
        shareddb.set_common_writer(db, project, True)

    common = server.sql(shareddb.COMMON_DB)
    assert 'GRANT CREATE ON SCHEMA public TO "app_supplier_pool"' in common
    assert ('ALTER DEFAULT PRIVILEGES FOR ROLE "app_supplier_pool" IN SCHEMA public'
            ' GRANT SELECT ON TABLES TO "common_reader"') in common
    assert not any('"app_supplier_pool_dev"' in s and "GRANT CREATE" in s for s in common)
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in common
    assert 'REVOKE CONNECT ON DATABASE "paas_common" FROM PUBLIC' in server.sql()


def test_deleting_a_project_keeps_its_database(server):
    """프로젝트를 지워도 DB는 남는다 — 같은 이름으로 새로 만든 앱이 그 데이터를 물려받지도 않는다."""
    c = TestClient(create_app())
    pid = _project()
    with SessionLocal() as db:
        shareddb.provision(db, db.get(Project, pid), BuildProfile.release)
    assert c.delete(f"{API}/projects/{pid}", headers=ADMIN).status_code == 204

    listing = c.get(f"{API}/shared-db", headers=ADMIN).json()["databases"]
    kept = next(d for d in listing if d["db_name"] == "app_supplier_pool")
    assert kept["orphan"] is True and kept["project_name"] == "supplier-pool"
    assert not any(s.startswith("DROP") for s in server.sql())

    again = _project()
    with SessionLocal() as db:
        row = shareddb.provision(db, db.get(Project, again), BuildProfile.release)
    assert row.db_name == f"app_supplier_pool_{again}"


def test_planning_agent_sees_how_to_use_it_and_what_is_already_there(server):
    """기획 에이전트는 사용 규약과 공용 자료 DB의 표를 받는다 — 있는 기준 정보를 다시 만들지 않게."""
    server.columns[shareddb.COMMON_DB] = [("suppliers", "id", "integer"), ("suppliers", "name", "text")]
    pid = _project()
    with SessionLocal() as db:
        project = db.get(Project, pid)
        resource = next(r for r in modules.available_resources(db, project)
                        if r["name"] == shareddb.MODULE_NAME)
        doc = planning.render_constraints_doc(planning.build_constraints(db, project))
    assert "{PREFIX}_DSN" in resource["usage"] and "읽기만" in resource["usage"]
    assert "## 공용 DB에 이미 있는 표" in doc
    assert "suppliers(id integer, name text)" in doc


def test_agents_read_common_data_through_the_db_mcp_server(server, monkeypatch, tmp_path):
    """DB 조회 MCP는 「공용 DB」를 목록 없이 연다 — 열리는 것은 공용 자료 DB(읽기 계정)뿐이다."""
    path = tmp_path / "common.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO suppliers (name) VALUES ('A사')")
    conn.commit()
    conn.close()
    with SessionLocal() as db:
        real = shareddb.reader_url(db)
    assert "paas_common_reader:" in real and real.endswith("/paas_common")
    monkeypatch.setattr(shareddb, "reader_url", lambda db: f"sqlite:///{path}")

    c = TestClient(create_app())
    rpc = lambda tool, args: c.post(f"{API}/mcp/db/{shareddb.MODULE_NAME}", headers=ADMIN, json={  # noqa: E731
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    assert "suppliers" in rpc("list_tables", {}).json()["result"]["content"][0]["text"]
    assert "A사" in rpc("run_select", {"sql": "SELECT name FROM suppliers"}).json()["result"]["content"][0]["text"]
    assert rpc("run_select", {"sql": "DELETE FROM suppliers"}).json()["error"]


def test_nightly_backup_keeps_the_last_few(server, monkeypatch, tmp_path):
    monkeypatch.setenv("PAAS_SHARED_DB_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("PAAS_SHARED_DB_BACKUP_KEEP", "2")
    monkeypatch.setenv("PAAS_PG_DUMP_PATH", "pg_dump")
    get_settings.cache_clear()
    seen: list = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw["env"].get("PGPASSWORD")))
        target = cmd[cmd.index("-f") + 1]
        open(target, "wb").write(b"dump")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(shareddb.subprocess, "run", fake_run)
    times = iter(["20261001-010000", "20261002-010000", "20261003-010000"])
    monkeypatch.setattr(shareddb, "datetime", type("D", (), {
        "now": staticmethod(lambda: type("T", (), {"__format__": lambda self, f: next(times)})())}))
    pid = _project()
    with SessionLocal() as db:
        shareddb.provision(db, db.get(Project, pid), BuildProfile.release)
        for _ in range(3):
            shareddb.backup(db, "app_supplier_pool")
        scheduler.reconcile(db)
        jobs = {j.name for j in db.query(ScheduledJob).all()}
    kept = sorted(p.name for p in (tmp_path / "app_supplier_pool").iterdir())
    assert kept == ["app_supplier_pool-20261002-010000.dump", "app_supplier_pool-20261003-010000.dump"]
    assert seen[0][1] == "adminpw" and "adminpw" not in " ".join(seen[0][0])  # 비밀번호는 인자에 없다
    assert {"db_backup:app_supplier_pool", "db_backup:paas_common"} <= jobs


def test_project_screen_and_common_write_api(server):
    c = TestClient(create_app())
    pid = _project()
    out = c.get(f"{API}/projects/{pid}/shared-db", headers=ADMIN).json()
    assert out == {"configured": True, "bound": True, "env_key": "DB_DSN", "common_db": "paas_common",
                   "databases": []}
    with SessionLocal() as db:
        shareddb.provision(db, db.get(Project, pid), BuildProfile.release)
    out = c.post(f"{API}/projects/{pid}/shared-db/common-write", json={"on": True}, headers=ADMIN).json()
    assert out["databases"][0]["common_write"] is True
    assert c.post(f"{API}/projects/{pid}/shared-db/common-write", json={"on": True}).status_code == 401


def test_nothing_appears_when_not_configured(fresh_settings, monkeypatch):
    monkeypatch.setenv("PAAS_SHARED_DB_ADMIN_URL", "")
    get_settings.cache_clear()
    create_app()
    with SessionLocal() as db:
        assert db.query(Module).filter(Module.name == shareddb.MODULE_NAME).count() == 0
        assert shareddb.ensure_module(db) is None
