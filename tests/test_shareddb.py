"""공용 DB — 부서마다 쓰는 DB를 여럿 고르고, 과제마다 스키마, 같은 DB를 고른 부서끼리 서로 읽고, 고르지 않은
부서는 그 DB에 접속하지도 못한다. DB의 용도 설명은 LLM이 적는다.

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
    BuildProfile, DataDatabase, DataGrant, DataSchema, Module, ModuleBinding, Organization, Project, ProjectDatabase,
    ScheduledJob,
)
from app.services import deployer, modules, planning, scheduler, shareddb

ADMIN = {"x-api-key": "test-admin-key"}
API = "/paas/api/v1"
ADMIN_URL = "postgresql+psycopg://paas_admin:adminpw@127.0.0.1:5432/postgres"
_TABLE_GRANT_RE = re.compile(r'(GRANT|REVOKE) ([A-Z, ]+) ON "(\w+)"\."(\w+)" (?:TO|FROM) "(\w+)"')
_USAGE_RE = re.compile(r'(GRANT|REVOKE) USAGE ON SCHEMA "(\w+)" (?:TO|FROM) "(\w+)"')
_MEMBER_RE = re.compile(r'(GRANT|REVOKE) "(reader_\w+)" (?:TO|FROM) "(\w+)"$')


class FakeServer:
    """계정·DB와, DB마다 표·표 권한·스키마 출입을 기억하고, 나간 SQL을 남긴다."""

    def __init__(self):
        self.roles: set[str] = set()
        self.dbs: set[str] = set()
        self.tables: dict[str, list[tuple[str, str, str]]] = {}         # DB → (스키마, 표, 설명)
        self.columns: dict[str, list[tuple[str, str, str, str]]] = {}   # DB → (스키마, 표, 열, 타입)
        self.privs: set[tuple[str, str, str, str, str]] = set()         # (DB, 계정, 스키마, 표, 권한)
        self.usage: set[tuple[str, str, str]] = set()                   # (DB, 계정, 스키마)
        self.members: set[tuple[str, str]] = set()                      # (DB 읽기 역할, 계정)
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
        if sql.startswith("SELECT count(*) FROM pg_class"):
            return [(len(self.tables.get(dbname, [])),)]
        if m := re.match(r'DROP DATABASE "(\w+)"', sql):
            self.dbs.discard(m.group(1))
        if m := re.match(r'DROP ROLE "(\w+)"', sql):
            self.roles.discard(m.group(1))
        if sql.startswith("SELECT r.rolname, m.rolname FROM pg_auth_members"):
            return list(self.members)
        if m := _MEMBER_RE.match(sql):
            verb, reader, role = m.groups()
            (self.members.add if verb == "GRANT" else self.members.discard)((reader, role))
            return []
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


def _dbs(org_id: int, *names: str):
    with SessionLocal() as db:
        return shareddb.set_databases(db, org_id, list(names))


def test_each_app_gets_its_own_schema_and_only_its_db_readers_can_enter(server):
    """과제는 부서의 기본 DB(paas_data)에 자기 스키마를 받는다 — 그 DB를 고른 부서의 과제(읽기 역할)만 들어가 읽는다.
    DB 접속도 읽기 역할에만 준다(예전처럼 모든 앱에 주지 않는다)."""
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
    reader = "reader_paas_data"
    assert f'GRANT USAGE ON SCHEMA "supplier_pool" TO "{reader}"' in data
    assert not any("USAGE ON SCHEMA" in s and '"paas_app"' in s for s in data)
    assert not any("supplier_pool_dev" in s and reader in s for s in data)  # 개발 스키마는 공유하지 않는다
    admin = server.sql()
    assert f'GRANT CONNECT ON DATABASE "paas_data" TO "{reader}"' in admin
    assert not any(s.startswith("GRANT CONNECT") and '"paas_app"' in s for s in admin)
    assert {(reader, "app_supplier_pool"), (reader, "app_supplier_pool_dev")} <= server.members
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


def test_departments_that_pick_the_same_db_read_each_other(server):
    """같은 DB를 고른 부서의 과제끼리는 서로의 운영 표를 읽는다 — 데이터는 옮기지 않는다. 한 부서만 고른 DB에는 그
    부서만 접속하고, 다른 부서에는 표 이름도 보이지 않는다(예전 「분리」)."""
    gp, team, hr = _org("GP실"), _org("GP Sourcing3팀"), _org("인사팀")
    upper, mine = _project("supplier-pool", gp), _project("sourcing-app", team)
    _dbs(hr, "paas_hr")
    secret = _project("payroll", hr)
    for pid in (upper, mine, secret):
        _provision(pid)
    server.tables[shareddb.DATA_DB] = [("supplier_pool", "suppliers", "협력사 기준 정보")]
    server.tables["paas_hr"] = [("payroll", "salaries", "급여")]
    with SessionLocal() as db:
        assert [t["table"] for t in shareddb.readable(db, db.get(Project, mine))["shared"]] == ["suppliers"]
        row = db.query(DataSchema).filter(DataSchema.project_id == secret).one()
        assert row.db_name == "paas_hr" and shareddb.dsn(row).endswith("/paas_hr")
        groups = shareddb.readable(db, db.get(Project, mine))
        assert not any(t["db_name"] == "paas_hr" for g in groups.values() for t in g)   # 이름도 보이지 않는다
        with pytest.raises(shareddb.SharedDbError, match="paas_hr을 쓰지 않습니다"):
            shareddb.grant(db, "payroll", "salaries", db.get(Project, mine), "read", "admin")
        assert {"paas_data", "paas_hr"} <= set(shareddb.backup_targets(db))
    assert 'GRANT CONNECT ON DATABASE "paas_hr" TO "reader_paas_hr"' in server.sql()
    assert ("reader_paas_hr", "app_sourcing_app") not in server.members
    assert ("reader_paas_data", "app_payroll") not in server.members

    # 인사팀이 기본 DB도 쓰기로 하면 — 그 DB의 읽기 역할에 든다(자기 스키마는 그대로 paas_hr)
    _dbs(hr, "paas_hr", "paas_data")
    assert ("reader_paas_data", "app_payroll") in server.members
    with SessionLocal() as db:
        assert [t["table"] for t in shareddb.readable(db, db.get(Project, secret))["shared"]] == ["suppliers"]
    _dbs(hr, "paas_hr")
    assert ("reader_paas_data", "app_payroll") not in server.members   # 빼면 거둔다


def test_a_department_can_use_several_dbs_and_the_project_picks_one(server):
    """부서가 DB를 여럿 쓰면 과제는 그중 하나에 스키마를 둔다(기획 LLM이 고른다 — 비어 있으면 용도도 적는다). 다른
    DB는 같은 계정으로 {PREFIX}_DSN_{DB}로 접속해 읽는다."""
    cost = _org("원가팀")
    _dbs(cost, "paas_data", "paas_cost")
    pid = _project("cost-db", cost)
    with SessionLocal() as db:
        project = db.get(Project, pid)
        assert [d["name"] for d in shareddb.database_choices(db, project)] == ["paas_data", "paas_cost"]
        assert shareddb.choose_db(db, project, "paas_cost", "품목별 원가·단가 자료") == "paas_cost"
        with pytest.raises(shareddb.SharedDbError, match="부서가 쓰는 DB가 아닙니다"):
            shareddb.choose_db(db, project, "paas_hr")
        env = deployer.resolve_env(db, project, BuildProfile.release)
        assert env["DB_DSN"].endswith("/paas_cost") and env["DB_DSN_PAAS_DATA"].endswith("/paas_data")
        assert env["DB_DSN"].split("@")[0] == env["DB_DSN_PAAS_DATA"].split("@")[0]   # 같은 계정
        assert db.get(DataDatabase, "paas_cost").description == "품목별 원가·단가 자료"
        assert db.get(DataDatabase, "paas_cost").described_by == "planning"
        with pytest.raises(shareddb.SharedDbError, match="이미 paas_cost에 있습니다"):
            shareddb.choose_db(db, project, "paas_data")                 # 자료가 생긴 뒤에는 옮기지 않는다
    assert {("reader_paas_cost", "app_cost_db"), ("reader_paas_data", "app_cost_db")} <= server.members


def test_a_db_holding_the_departments_data_cannot_be_removed(server):
    """자기 과제의 자료가 있는 DB를 부서에서 빼는 설정은 거부한다 — 스키마를 옮기는 일은 되돌리기 어렵다."""
    hr = _org("인사팀")
    _provision(_project("payroll", hr))
    with pytest.raises(shareddb.SharedDbError, match="뺄 수 없습니다: paas_data"):
        _dbs(hr, "paas_hr")
    with pytest.raises(shareddb.SharedDbError, match="영소문자"):
        _dbs(hr, "Bad Name!")
    with pytest.raises(shareddb.SharedDbError, match="하나 이상"):
        _dbs(hr)
    with SessionLocal() as db:
        assert db.query(DataSchema).filter(DataSchema.db_name != shareddb.DATA_DB).count() == 0


def test_only_a_db_without_data_can_be_deleted(server):
    """DB 삭제는 자료가 없는 DB만 — 기본 DB, 과제 스키마가 있는 DB, 서버에 표가 있는 DB는 거부한다. 지우면 서버의 DB와
    읽기 역할을 없애고, 쓰던 부서와 골라 둔 과제(스키마 없음)에서 뺀다."""
    hr, cost = _org("인사팀"), _org("원가팀")
    _dbs(hr, "paas_hr", "paas_data")
    _dbs(cost, "paas_data", "paas_spare", "paas_outside")
    _provision(_project("payroll", hr))
    waiting = _project("cost-plan", cost)
    with SessionLocal() as db:
        shareddb.choose_db(db, db.get(Project, waiting), "paas_spare")
        db.query(DataSchema).filter(DataSchema.db_name == "paas_data").update({"db_name": "paas_hr"})
        db.commit()
        with pytest.raises(shareddb.SharedDbError, match="기본 DB"):
            shareddb.delete_database(db, "paas_data")
        with pytest.raises(shareddb.SharedDbError, match="과제 스키마가 1개 있습니다.*강제 삭제로"):
            shareddb.delete_database(db, "paas_hr")
        server.tables["paas_outside"] = [("public", "legacy", "")]
        with pytest.raises(shareddb.SharedDbError, match="표가 1개 있습니다.*강제 삭제로"):
            shareddb.delete_database(db, "paas_outside")
        rows = shareddb.delete_database(db, "paas_spare")
        assert "paas_spare" not in {r["name"] for r in rows}
        assert shareddb.databases_of(db, cost) == ["paas_data", "paas_outside"]
        assert db.get(ProjectDatabase, waiting) is None                    # 골라 둔 과제는 부서의 기본으로
        assert db.get(DataDatabase, "paas_spare") is None
    assert 'DROP DATABASE "paas_spare"' in server.sql() and 'DROP ROLE "reader_paas_spare"' in server.sql()
    assert "paas_spare" not in server.dbs
    c = TestClient(create_app())
    assert c.delete(f"{API}/shared-db/databases/paas_outside").status_code == 401
    refused = c.delete(f"{API}/shared-db/databases/paas_hr", headers=ADMIN)
    assert refused.status_code == 409 and "강제 삭제로" in refused.json()["detail"]
    assert c.delete(f"{API}/shared-db/databases/paas_data", params={"force": "true"}, headers=ADMIN).status_code == 409


def test_force_delete_drops_the_db_with_its_schemas(server):
    """강제 삭제 — 스키마가 있어도 접속을 끊고 DB를 통째로 지운다. 그 DB의 스키마 기록과 표 권한도 지우고, 그 과제를
    다시 배포하면 부서의 기본 DB에 빈 스키마를 받는다."""
    hr, cost = _org("인사팀"), _org("원가팀")
    _dbs(hr, "paas_hr", "paas_data")
    _dbs(cost, "paas_hr", "paas_data")
    payroll, reader = _project("payroll", hr), _project("cost-db", cost)
    with SessionLocal() as db:
        shareddb.choose_db(db, db.get(Project, payroll), "paas_hr")
    _provision(payroll, BuildProfile.release, BuildProfile.development)
    _provision(reader)
    server.tables["paas_hr"] = [("payroll", "salaries", "급여")]
    with SessionLocal() as db:
        shareddb.grant(db, "payroll", "salaries", db.get(Project, reader), "write", "admin")
    c = TestClient(create_app())
    out = c.delete(f"{API}/shared-db/databases/paas_hr", params={"force": "true"}, headers=ADMIN)
    assert out.status_code == 200, out.text
    assert 'DROP DATABASE "paas_hr" WITH (FORCE)' in server.sql()
    with SessionLocal() as db:
        assert db.query(DataSchema).filter(DataSchema.db_name == "paas_hr").count() == 0
        assert db.query(DataGrant).count() == 0
        assert shareddb.databases_of(db, hr) == ["paas_data"]
        row = shareddb.provision(db, db.get(Project, payroll), BuildProfile.release)   # 다시 배포하면
        assert row.db_name == "paas_data" and row.schema_name == "payroll"


def test_old_sharing_unit_grants_are_withdrawn(server):
    """예전 「공유 단위」 읽기가 남은 스키마는 대조에서 거두고 비운다."""
    pid = _project("supplier-pool", _org("GP실"))
    _provision(pid)
    with SessionLocal() as db:
        row = db.query(DataSchema).filter(DataSchema.project_id == pid).one()
        row.unit_id = 7
        db.commit()
        shareddb.reconcile(db)
        assert db.query(DataSchema).filter(DataSchema.project_id == pid).one().unit_id is None
    assert 'REVOKE "unit_7_reader" FROM "app_supplier_pool"' in server.sql()
    assert 'REVOKE USAGE ON SCHEMA "supplier_pool" FROM "unit_7_reader"' in server.sql(shareddb.DATA_DB)


def test_db_purpose_is_rewritten_by_the_llm_only_when_its_use_changed(server, monkeypatch):
    """DB의 용도 설명은 LLM이 실제 쓰임(표·표 설명·쓰는 과제)을 보고 적는다 — 바뀐 것이 없으면 부르지 않는다."""
    from app.services import llm
    asked = []
    monkeypatch.setattr(llm, "default_provider", lambda db: object())
    monkeypatch.setattr(llm, "chat_completion", lambda provider, messages, db=None, *a, **k: (
        asked.append(messages[0]["content"]) or "「협력사 기준 정보와 견적 자료를 모으는 DB」"))
    _provision(_project("supplier-pool", _org("GP실")))
    server.tables[shareddb.DATA_DB] = [("supplier_pool", "suppliers", "협력사 기준 정보")]
    with SessionLocal() as db:
        assert shareddb.reconcile(db)["described"] == ["paas_data"]
        row = db.get(DataDatabase, "paas_data")
        assert row.description == "협력사 기준 정보와 견적 자료를 모으는 DB" and row.described_by == "llm"
        assert "supplier_pool.suppliers — 협력사 기준 정보" in asked[0] and "GP실" in asked[0]
        assert shareddb.reconcile(db)["described"] == []           # 그대로면 다시 묻지 않는다
        server.tables[shareddb.DATA_DB].append(("supplier_pool", "quotes", "견적"))
        assert shareddb.reconcile(db)["described"] == ["paas_data"]
    assert len(asked) == 2


def test_tables_are_granted_per_table_and_reconciled(server):
    """쓰기(와 표 단위 읽기)는 관리자가 표마다 준다 — 스키마 출입도 함께, 개발 계정은 읽기만. 손으로 준 권한·
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


def test_planning_agent_sees_its_dbs_with_purposes_and_readable_tables(server):
    """기획 에이전트는 부서가 쓰는 DB와 그 용도 설명, 읽을 수 있는 표(자기·같은 DB·받은 것)를 열까지 본다 — 부서가
    쓰지 않는 DB의 표는 보이지 않는다. 바인딩 도구로 DB를 고르고, 비어 있으면 용도를 적는다."""
    gp, cost = _org("GP실"), _org("원가팀")
    _dbs(cost, "paas_cost")
    me, sibling, other = _project("sourcing-app", gp), _project("supplier-pool", gp), _project("cost-db", cost)
    for pid in (me, sibling, other):
        _provision(pid)
    server.tables[shareddb.DATA_DB] = [("sourcing_app", "rfqs", ""), ("supplier_pool", "suppliers", "협력사 기준 정보")]
    server.tables["paas_cost"] = [("cost_db", "unit_costs", "품목별 단가")]
    server.columns[shareddb.DATA_DB] = [("sourcing_app", "rfqs", "id", "integer"),
                                        ("supplier_pool", "suppliers", "name", "text")]
    with SessionLocal() as db:
        db.get(DataDatabase, "paas_data").description = "구매·협력사 자료"
        db.commit()
        project = db.get(Project, me)
        resource = next(r for r in modules.available_resources(db, project) if r["name"] == shareddb.MODULE_NAME)
        doc = planning.render_constraints_doc(planning.build_constraints(db, project))
    assert "{PREFIX}_DSN" in resource["usage"] and "COMMENT ON TABLE" in resource["usage"]
    assert resource["databases"] == [{"name": "paas_data", "description": "구매·협력사 자료", "current": True}]
    assert "DB paas_data (이 과제의 DB): 구매·협력사 자료" in doc
    assert "[paas_data] sourcing_app.rfqs(id integer)" in doc
    assert "[paas_data] supplier_pool.suppliers(name text) — 협력사 기준 정보" in doc   # 같은 DB: 열까지
    assert "unit_costs" not in doc and "paas_cost" not in doc                          # 쓰지 않는 DB는 안 보인다

    # 바인딩 도구 — DB를 고른다(부서의 DB 중에서만)
    new = _project("cost-report", cost)
    with SessionLocal() as db:
        db.query(ModuleBinding).filter(ModuleBinding.project_id == new).delete()
        db.commit()
        project = db.get(Project, new)
        tools = planning.module_bind_tools(modules.available_resources(db, project))
        props = tools[0]["function"]["parameters"]["properties"]
        assert props["database"]["enum"] == ["paas_cost"]
        run = planning.make_bind_executor(db, project, "agent", [])
        assert "DB를 고르지 못했습니다" in run("bind_module", {"module_name": shareddb.MODULE_NAME, "env_prefix": "DB",
                                                              "database": "paas_data"})
        out = run("bind_module", {"module_name": shareddb.MODULE_NAME, "env_prefix": "DB", "database": "paas_cost",
                                  "database_purpose": "원가 자료"})
        assert "스키마는 DB paas_cost에 둔다" in out
        assert db.get(DataDatabase, "paas_cost").description == "원가 자료"


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


def test_admin_apis_for_grants_and_department_dbs(server):
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

    assert c.put(f"{API}/shared-db/organizations/{team}/databases", json={"databases": ["paas_t"]}).status_code == 401
    rows = c.put(f"{API}/shared-db/organizations/{team}/databases", json={"databases": ["paas_t", "paas_data"]},
                 headers=ADMIN).json()
    assert next(r for r in rows if r["organization_id"] == team)["databases"] == ["paas_t", "paas_data"]
    refused = c.put(f"{API}/shared-db/organizations/{gp}/databases", json={"databases": ["paas_t"]}, headers=ADMIN)
    assert refused.status_code == 409 and "뺄 수 없습니다" in refused.json()["detail"]
    listed = c.get(f"{API}/shared-db/databases", headers=ADMIN).json()
    assert {d["name"] for d in listed["databases"]} >= {"paas_data", "paas_t"}
    assert next(d for d in listed["databases"] if d["name"] == "paas_t")["organizations"] == ["GP Sourcing3팀"]
    theirs = c.get(f"{API}/projects/{reader}/shared-db", headers=ADMIN).json()
    assert [d["name"] for d in theirs["databases"]] == ["paas_data"]


def test_nothing_appears_when_not_configured(fresh_settings, monkeypatch):
    monkeypatch.setenv("PAAS_SHARED_DB_ADMIN_URL", "")
    get_settings.cache_clear()
    create_app()
    with SessionLocal() as db:
        assert db.query(Module).filter(Module.name == shareddb.MODULE_NAME).count() == 0
        assert shareddb.ensure_module(db) is None
