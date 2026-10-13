"""공용 DB — 과제마다 스키마, 같은 공유 단위는 서로 읽고, 단위 밖은 스키마에 들어가지도 못한다.

PostgreSQL이 없는 곳에서 돈다: DB에 닿는 유일한 자리(shareddb._execute)를 가짜 서버로 바꾸고,
무엇을 어떤 순서로 만들고 줬는지(나간 SQL·표 권한·스키마 출입)를 본다. 실제 서버에서의 동작은
infra/postgres/README.md의 점검 절차로 확인한다.
"""
import re
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import create_app
from app.models import (
    BuildProfile, DataGrant, DataSchema, Module, ModuleBinding, Organization, Project, ScheduledJob,
)
from app.services import deployer, modules, planning, scheduler, shareddb

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"
ADMIN_URL = "postgresql+psycopg://paas_admin:adminpw@127.0.0.1:5432/postgres"
_TABLE_GRANT_RE = re.compile(r'(GRANT|REVOKE) ([A-Z, ]+) ON "(\w+)"\."(\w+)" (?:TO|FROM) "(\w+)"')
_USAGE_RE = re.compile(r'(GRANT|REVOKE) USAGE ON SCHEMA "(\w+)" (?:TO|FROM) "(\w+)"')


class FakeServer:
    """계정·DB와, DB마다 표·표 권한·스키마 출입을 기억하고, 나간 SQL을 남긴다."""

    def __init__(self):
        self.roles: set[str] = set()
        self.dbs: set[str] = set()
        self.tables: dict[str, list[tuple[str, str, str]]] = {}         # DB → (스키마, 표, 설명)
        self.columns: dict[str, list[tuple[str, str, str, str]]] = {}   # DB → (스키마, 표, 열, 타입)
        self.privs: set[tuple[str, str, str, str, str]] = set()         # (DB, 계정, 스키마, 표, 권한)
        self.usage: set[tuple[str, str, str]] = set()                   # (DB, 계정, 스키마)
        self.log: list[tuple[str | None, str]] = []

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
        if sql.startswith("SELECT n.nspname, c.relname"):
            return list(self.tables.get(dbname, []))
        if sql.startswith("SELECT table_schema, table_name, column_name"):
            return list(self.columns.get(dbname, []))
        if sql.startswith("SELECT grantee, table_schema"):
            return [(r, s, t, p) for d, r, s, t, p in self.privs if d == dbname]
        if sql.startswith("SELECT n.nspname, r.rolname"):
            return [(s, r) for d, r, s in self.usage if d == dbname]
        if m := _USAGE_RE.match(sql):
            verb, schema, role = m.groups()
            (self.usage.add if verb == "GRANT" else self.usage.discard)((dbname, role, schema))
        elif m := _TABLE_GRANT_RE.match(sql):
            verb, privs, schema, table, role = m.groups()
            for p in (x.strip() for x in privs.split(",")):
                (self.privs.add if verb == "GRANT" else self.privs.discard)((dbname, role, schema, table, p))
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


def _org(name: str) -> int:
    with SessionLocal() as db:
        org = Organization(name=name)
        db.add(org)
        db.commit()
        return org.id


def _project(name: str, org_id: int | None = None) -> int:
    with SessionLocal() as db:
        project = Project(name=name, type="react", git_url=f"https://git.test/{name}",
                          organization_id=org_id)
        db.add(project)
        db.flush()
        module = db.query(Module).filter(Module.name == shareddb.MODULE_NAME).one()
        db.add(ModuleBinding(project_id=project.id, module_id=module.id, env_prefix="DB"))
        db.commit()
        return project.id


def _provision(pid: int, *profiles: BuildProfile) -> None:
    with SessionLocal() as db:
        for profile in profiles or (BuildProfile.release,):
            shareddb.provision(db, db.get(Project, pid), profile)


def _policy(org_id: int, share_with: int | None = None, isolated: bool = False):
    with SessionLocal() as db:
        return shareddb.set_policy(db, org_id, share_with, isolated)


def test_each_app_gets_its_own_schema_and_only_its_unit_can_enter(server):
    """과제는 자기 스키마를 받는다 — 같은 공유 단위만 그 스키마에 들어가 읽고, 다른 앱은 들어가지 못한다."""
    gp = _org("GP실")
    pid = _project("supplier-pool", gp)
    with SessionLocal() as db:
        project = db.get(Project, pid)
        release = deployer.resolve_env(db, project, BuildProfile.release)["DB_DSN"]
        dev = deployer.resolve_env(db, project, BuildProfile.development)["DB_DSN"]
        rows = db.query(DataSchema).filter(DataSchema.project_id == pid).all()

    assert re.fullmatch(r"postgresql://app_supplier_pool:[\w-]+@127\.0\.0\.1:5432/paas_data", release)
    assert dev.startswith("postgresql://app_supplier_pool_dev:")
    data = server.sql(shareddb.DATA_DB)
    assert 'CREATE SCHEMA IF NOT EXISTS "supplier_pool" AUTHORIZATION "app_supplier_pool"' in data
    reader = f"unit_{gp}_reader"
    assert f'GRANT USAGE ON SCHEMA "supplier_pool" TO "{reader}"' in data
    # 모든 앱(paas_app)에는 스키마 출입을 주지 않는다 — 단위 밖은 들어가지도 못한다
    assert not any("USAGE ON SCHEMA" in s and '"paas_app"' in s for s in data)
    assert not any("supplier_pool_dev" in s and reader in s for s in data)  # 개발 스키마는 공유하지 않는다
    admin = server.sql()
    assert 'ALTER ROLE "app_supplier_pool" IN DATABASE "paas_data" SET search_path TO "supplier_pool"' in admin
    assert 'ALTER ROLE "app_supplier_pool" CONNECTION LIMIT 10' in admin
    assert "ALTER ROLE \"app_supplier_pool\" SET statement_timeout = '60s'" in admin
    password = release.split(":")[2].split("@")[0]
    assert all(password not in r.password_encrypted for r in rows)


def test_provisioning_is_idempotent(server):
    pid = _project("supplier-pool", _org("GP실"))
    with SessionLocal() as db:
        project = db.get(Project, pid)
        first = shareddb.dsn(shareddb.provision(db, project, BuildProfile.release))
        before = len([s for s in server.sql() if s.startswith("CREATE")])
        second = shareddb.dsn(shareddb.provision(db, project, BuildProfile.release))
    assert first == second
    assert len([s for s in server.sql() if s.startswith("CREATE")]) == before


def test_sharing_unit_can_be_widened_to_an_upper_department(server):
    """관리자가 팀의 공유 단위를 상위 부서로 지정하면 그 부서와 서로 읽는다 — 데이터는 옮기지 않는다."""
    gp, team = _org("GP실"), _org("GP Sourcing3팀")
    upper, mine = _project("supplier-pool", gp), _project("sourcing-app", team)
    _provision(upper)
    _provision(mine)
    server.tables[shareddb.DATA_DB] = [("supplier_pool", "suppliers", "협력사 기준 정보")]
    with SessionLocal() as db:
        assert shareddb.readable(db, db.get(Project, mine))["unit"] == []

    _policy(team, share_with=gp)
    assert f'REVOKE "unit_{team}_reader" FROM "app_sourcing_app"' in server.sql()
    assert f'GRANT "unit_{gp}_reader" TO "app_sourcing_app"' in server.sql()
    assert f'REVOKE USAGE ON SCHEMA "sourcing_app" FROM "unit_{team}_reader"' in server.sql(shareddb.DATA_DB)
    with SessionLocal() as db:
        assert [t["table"] for t in shareddb.readable(db, db.get(Project, mine))["unit"]] == ["suppliers"]
        rows = shareddb.policy_rows(db)
    assert next(r for r in rows if r["organization_id"] == team)["unit_name"] == "GP실"

    with pytest.raises(shareddb.SharedDbError, match="자기 자신"):
        _policy(gp, share_with=gp)
    with pytest.raises(shareddb.SharedDbError, match="돌고"):
        _policy(gp, share_with=team)


def test_an_isolated_unit_gets_its_own_database_that_only_it_can_reach(server):
    """분리된 단위는 자기 DB를 쓴다 — 그 DB에는 그 단위만 접속하고, 표 권한도 밖으로 나가지 않는다."""
    hr, cost = _org("인사팀"), _org("원가팀")
    _policy(hr, isolated=True)
    secret, other = _project("payroll", hr), _project("cost-db", cost)
    _provision(secret)
    _provision(other)
    with SessionLocal() as db:
        row = db.query(DataSchema).filter(DataSchema.project_id == secret).one()
        assert row.db_name == f"paas_org_{hr}"
        assert shareddb.dsn(row).endswith(f"/paas_org_{hr}")
    admin = server.sql()
    assert f'GRANT CONNECT ON DATABASE "paas_org_{hr}" TO "unit_{hr}_reader"' in admin
    assert f'GRANT CONNECT ON DATABASE "paas_org_{hr}" TO "paas_app"' not in admin

    server.tables[f"paas_org_{hr}"] = [("payroll", "salaries", "급여")]
    with SessionLocal() as db:
        # 다른 단위에는 이름도 내보내지 않는다
        assert shareddb.readable(db, db.get(Project, other))["requestable"] == []
        with pytest.raises(shareddb.SharedDbError, match="밖으로 줄 수 없습니다"):
            shareddb.grant(db, "payroll", "salaries", db.get(Project, other), "read", "admin")
        assert {"paas_data", f"paas_org_{hr}"} <= set(shareddb.backup_targets(db))


def test_a_setting_that_would_move_existing_data_to_another_database_is_refused(server):
    """자료가 있는 부서의 DB가 바뀌는 설정은 거부한다 — 스키마를 옮기는 일은 되돌리기 어렵다."""
    hr = _org("인사팀")
    _provision(_project("payroll", hr))
    with pytest.raises(shareddb.SharedDbError, match="자료가 있는 부서의 DB가 바뀌는"):
        _policy(hr, isolated=True)
    with SessionLocal() as db:
        assert db.query(DataSchema).filter(DataSchema.db_name != shareddb.DATA_DB).count() == 0


def test_other_units_tables_are_granted_per_table_and_reconciled(server):
    """단위 밖의 표는 관리자가 표마다 준다 — 스키마 출입도 함께, 개발 계정은 읽기만. 손으로 준 권한·
    만료된 권한은 대조에서 거둔다(스키마 출입까지)."""
    gp, cost = _org("GP실"), _org("원가팀")
    owner, reader = _project("supplier-pool", gp), _project("cost-db", cost)
    _provision(owner)
    _provision(reader, BuildProfile.release, BuildProfile.development)
    server.tables[shareddb.DATA_DB] = [("supplier_pool", "suppliers", "협력사 기준 정보")]
    with SessionLocal() as db:
        with pytest.raises(shareddb.SharedDbError, match="표가 없습니다"):
            shareddb.grant(db, "supplier_pool", "nope", db.get(Project, reader), "read", "admin")
        shareddb.grant(db, "supplier_pool", "suppliers", db.get(Project, reader), "write", "admin")
    assert {(r, p) for d, r, s, t, p in server.privs} == {
        ("app_cost_db", "SELECT"), ("app_cost_db", "INSERT"), ("app_cost_db", "UPDATE"),
        ("app_cost_db", "DELETE"), ("app_cost_db_dev", "SELECT")}
    assert {(r, s) for d, r, s in server.usage if r.startswith("app_")} == {
        ("app_cost_db", "supplier_pool"), ("app_cost_db_dev", "supplier_pool")}

    server.privs.add((shareddb.DATA_DB, "app_cost_db_dev", "supplier_pool", "suppliers", "DELETE"))
    with SessionLocal() as db:
        assert shareddb.reconcile(db)["revoked"] == 1

    with SessionLocal() as db:
        db.query(DataGrant).one().expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        out = shareddb.reconcile(db)
    assert out["expired"] == 1 and server.privs == set()
    assert not any(r.startswith("app_") for d, r, s in server.usage)


def test_planning_agent_sees_readable_tables_and_what_it_could_request(server):
    """기획 에이전트는 읽을 수 있는 표(자기·같은 공유 단위·받은 것)는 열까지, 단위 밖은 이름·설명만."""
    gp, cost = _org("GP실"), _org("원가팀")
    me, sibling, other = _project("sourcing-app", gp), _project("supplier-pool", gp), _project("cost-db", cost)
    for pid in (me, sibling, other):
        _provision(pid)
    server.tables[shareddb.DATA_DB] = [("sourcing_app", "rfqs", ""), ("supplier_pool", "suppliers", "협력사 기준 정보"),
                                       ("cost_db", "unit_costs", "품목별 단가")]
    server.columns[shareddb.DATA_DB] = [("sourcing_app", "rfqs", "id", "integer"),
                                        ("supplier_pool", "suppliers", "name", "text"),
                                        ("cost_db", "unit_costs", "price", "numeric")]
    with SessionLocal() as db:
        project = db.get(Project, me)
        resource = next(r for r in modules.available_resources(db, project) if r["name"] == shareddb.MODULE_NAME)
        doc = planning.render_constraints_doc(planning.build_constraints(db, project))
    assert "{PREFIX}_DSN" in resource["usage"] and "COMMENT ON TABLE" in resource["usage"]
    assert "sourcing_app.rfqs(id integer)" in doc
    assert "supplier_pool.suppliers(name text) — 협력사 기준 정보" in doc   # 같은 공유 단위: 열까지
    assert "cost_db.unit_costs — 품목별 단가" in doc                        # 단위 밖: 이름·설명만
    assert "price numeric" not in doc


def test_deleting_a_project_keeps_its_schema(server):
    c = TestClient(create_app())
    pid = _project("supplier-pool")
    _provision(pid)
    assert c.delete(f"{API}/projects/{pid}", headers=ADMIN).status_code == 204
    schemas = c.get(f"{API}/shared-db", headers=ADMIN).json()["schemas"]
    kept = next(s for s in schemas if s["schema"] == "supplier_pool")
    assert kept["orphan"] is True and kept["project_name"] == "supplier-pool"
    assert not any(s.startswith("DROP") for s in server.sql() + server.sql(shareddb.DATA_DB))
    again = _project("supplier-pool")
    with SessionLocal() as db:
        row = shareddb.provision(db, db.get(Project, again), BuildProfile.release)
    assert row.schema_name == f"supplier_pool_{again}"


def test_db_mcp_server_does_not_open_the_shared_db(server):
    c = TestClient(create_app())
    r = c.post(f"{API}/mcp/db/{shareddb.MODULE_NAME}", headers=ADMIN,
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 403


def test_nightly_backup_and_hourly_reconcile_are_scheduled(server, monkeypatch, tmp_path):
    monkeypatch.setenv("PAAS_SHARED_DB_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("PAAS_SHARED_DB_BACKUP_KEEP", "2")
    monkeypatch.setenv("PAAS_PG_DUMP_PATH", "pg_dump")
    get_settings.cache_clear()
    seen: list = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw["env"].get("PGPASSWORD")))
        open(cmd[cmd.index("-f") + 1], "wb").write(b"dump")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(shareddb.subprocess, "run", fake_run)
    times = iter(["20261001-010000", "20261002-010000", "20261003-010000"])
    monkeypatch.setattr(shareddb, "datetime", type("D", (), {
        "now": staticmethod(lambda *a: type("T", (), {"__format__": lambda self, f: next(times)})())}))
    with SessionLocal() as db:
        for _ in range(3):
            shareddb.backup(db)
        scheduler.reconcile(db)
        before = {j.name for j in db.query(ScheduledJob).all()}
    _provision(_project("supplier-pool"))
    with SessionLocal() as db:
        scheduler.reconcile(db)
        after = {j.name for j in db.query(ScheduledJob).all()}
    kept = sorted(p.name for p in (tmp_path / "paas_data").iterdir())
    assert kept == ["paas_data-20261002-010000.dump", "paas_data-20261003-010000.dump"]
    assert seen[0][1] == "adminpw" and "adminpw" not in " ".join(seen[0][0])
    assert not {"db_backup:paas_data", "db_grants"} & before
    assert {"db_backup:paas_data", "db_grants"} <= after


def test_admin_apis_for_grants_and_sharing_units(server):
    gp, team, cost = _org("GP실"), _org("GP Sourcing3팀"), _org("원가팀")
    owner, reader = _project("supplier-pool", gp), _project("cost-db", cost)
    _provision(owner)
    _provision(reader)
    server.tables[shareddb.DATA_DB] = [("supplier_pool", "suppliers", "협력사 기준 정보")]
    c = TestClient(create_app())
    body = {"schema_name": "supplier_pool", "table": "suppliers", "project_id": reader, "privilege": "read"}
    assert c.post(f"{API}/shared-db/grants", json=body).status_code == 401
    made = c.post(f"{API}/shared-db/grants", json=body, headers=ADMIN)
    assert made.status_code == 201, made.text
    mine = c.get(f"{API}/projects/{owner}/shared-db", headers=ADMIN).json()
    assert [(g["project_name"], g["privilege"]) for g in mine["given"]] == [("cost-db", "read")]
    theirs = c.get(f"{API}/projects/{reader}/shared-db", headers=ADMIN).json()
    assert [t["table"] for t in theirs["tables"]["granted"]] == ["suppliers"]
    assert c.delete(f"{API}/shared-db/grants/{made.json()['id']}", headers=ADMIN).status_code == 204

    assert c.put(f"{API}/shared-db/policies/{team}", json={"share_with_org_id": gp}).status_code == 401
    rows = c.put(f"{API}/shared-db/policies/{team}", json={"share_with_org_id": gp}, headers=ADMIN).json()
    assert next(r for r in rows if r["organization_id"] == team)["unit_id"] == gp
    refused = c.put(f"{API}/shared-db/policies/{gp}", json={"isolated": True}, headers=ADMIN)
    assert refused.status_code == 409 and "자료가 있는" in refused.json()["detail"]


def test_nothing_appears_when_not_configured(fresh_settings, monkeypatch):
    monkeypatch.setenv("PAAS_SHARED_DB_ADMIN_URL", "")
    get_settings.cache_clear()
    create_app()
    with SessionLocal() as db:
        assert db.query(Module).filter(Module.name == shareddb.MODULE_NAME).count() == 0
        assert shareddb.ensure_module(db) is None
