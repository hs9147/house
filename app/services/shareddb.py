"""공용 DB — 같은 서버의 PostgreSQL에 자료 DB를 여럿 두고, 과제마다 스키마를 주며, 부서 간 경계를 권한으로 막는다.

**구조.** 서버에는 자료 DB가 여럿이다(기본 paas_data). **부서는 쓰는 DB를 여럿 고른다**(OrgDatabase — 고르지
않으면 기본 DB 하나). 과제는 부서의 DB 중 하나에 자기 스키마를 둔다(ProjectDatabase — 기획 LLM이 DB의 용도
설명을 보고 고른다). 과제마다 스키마를 따로 두는 것은 표 이름과 마이그레이션 관리 표(alembic_version 등)가
과제끼리 부딪히지 않게 하려는 것이다. 스키마 이름은 과제 이름이다.

**읽는 범위 = 같은 DB를 고른 부서.** DB마다 읽기 역할(reader_{DB})이 있고, 그 DB를 고른 부서의 과제 계정이 그
역할에 든다 — 그 DB의 운영 스키마를 모두 읽는다. 그 DB를 고르지 않은 부서는 **접속하지도 못한다**(CONNECT는
읽기 역할에만). 한 부서만 고른 DB는 그 부서 전용이다(예전 「분리」). 쓰기는 자기 스키마와 관리자가 표 단위로
준 것(DataGrant)만. 과제는 자기 DB 밖의 DB(부서가 고른 다른 DB)에도 같은 계정으로 접속한다.

**DB의 용도 설명은 LLM이 적는다**(DataDatabase.description) — 기획에서 DB를 고를 때(비어 있으면), 그리고 권한
대조 때 DB 안의 표·표 설명·쓰는 과제가 바뀌었으면 다시. 사람이 입력하지 않는다.

**자료가 생긴 뒤에는 DB를 옮기지 않는다.** 부서가 자기 과제의 스키마가 있는 DB를 빼는 설정은 거부한다
(백업→복원은 안내서의 수동 절차로 둔다).

**권한의 원천은 플랫폼의 표다**(OrgDatabase·DataGrant). PostgreSQL 권한은 그 반영이고 reconcile이 주기적으로
맞춘다 — 손으로 준 권한과 만료된 권한은 거둔다. 막는 것은 DB 자체라 앱이 우회할 수 없다.

**관리 계정은 슈퍼유저가 아니다**(CREATEDB·CREATEROLE). 만든 앱 계정은 관리 계정에 GRANT한다 —
그래야 그 계정 소유의 스키마를 만들고, 백업·대조·스키마 요약 때 읽는다.

**지우지 않는다.** 바인딩을 풀어도, 과제를 지워도 스키마는 남는다 — 데이터는 되돌릴 수 없다.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    BuildProfile, DataDatabase, DataGrant, DataSchema, Module, ModuleType, Organization, OrgDatabase, Project,
    ProjectDatabase,
)
from ..security import decrypt_value, encrypt_value

MODULE_NAME = "shared-db"
DATA_DB = "paas_data"
APP_GROUP = "paas_app"   # 예전에 모든 앱 계정에 기본 DB 접속을 주던 역할 — 지금은 거둔다(읽기 역할만 접속)
_DB_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
PRIVILEGES = {"read": ("SELECT",), "write": ("SELECT", "INSERT", "UPDATE", "DELETE")}
_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
_NAME_RE = re.compile(r"[^a-z0-9]+")
# 표 수가 많아도 기획 문서의 컨텍스트를 덮지 않게 — 스키마 요약의 상한(글자).
SCHEMA_SUMMARY_CHARS = 6000
_BACKUP_TIMEOUT = 600

# 기획 에이전트가 읽는 사용 규약(available_resources) — 앱을 짓는 쪽이 알아야 하는 사실이다.
USAGE = (
    "공용 PostgreSQL DB의 이 앱 전용 스키마. 접속 정보는 환경변수 {PREFIX}_DSN(postgresql://…)으로"
    " 주입되고, 검색 경로가 자기 스키마로 잡혀 있어 표 이름만 쓰면 거기 만들어진다. 표는 앱이 기동할 때"
    " 마이그레이션(Alembic·Prisma·Flyway 등)으로 만들고, 표마다 COMMENT ON TABLE로 무엇을 담는지 적는다"
    "(다른 과제가 그 설명을 보고 쓴다). 개발 배포는 개발 스키마에 쓴다. 같은 DB를 쓰는 부서 과제의 표와"
    " 권한을 받은 표는 '스키마.표'로 읽는다(쓰기는 권한을 받은 표만). 부서가 다른 DB도 쓰면 그 DB는"
    " {PREFIX}_DSN_{DB이름(대문자)}으로 접속한다(같은 계정). 계정마다 연결 수와 질의 시간에"
    " 상한이 있다. 슈퍼유저 작업·확장 설치는 할 수 없다."
)


class SharedDbError(Exception):
    """사람에게 그대로 보여 줄 수 있는 사유."""


def configured() -> bool:
    return bool(get_settings().shared_db_admin_url.strip())


def _admin_url():
    raw = get_settings().shared_db_admin_url.strip()
    if not raw:
        raise SharedDbError("공용 DB가 설정되지 않았습니다 — PAAS_SHARED_DB_ADMIN_URL을 지정하세요.")
    return make_url(raw)


def _execute(dbname: str | None, sql: str) -> list[tuple]:
    """관리 계정으로 SQL 한 문장 — **이 모듈이 DB에 닿는 유일한 자리**다(시험은 여기를 바꾼다).

    CREATE DATABASE는 트랜잭션 안에서 돌지 않으므로 늘 AUTOCOMMIT이다. 호출마다 연결하고 닫는다
    (스키마 생성·권한 부여는 드물고, 풀을 들고 있으면 백업을 막는다).
    """
    from sqlalchemy import create_engine, text  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    url = _admin_url()
    if dbname:
        url = url.set(database=dbname)
    try:
        engine = create_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    except ModuleNotFoundError as e:
        raise SharedDbError(f"DB 드라이버가 없습니다({e}) — pip install 'psycopg[binary]'")
    try:
        with engine.connect() as conn:
            result = conn.execute(text(sql))
            return [tuple(r) for r in result] if result.returns_rows else []
    except Exception as e:  # noqa: BLE001 — 비밀번호가 실린 SQL은 사유에 싣지 않는다
        raise SharedDbError(f"공용 DB 작업 실패: {type(e).__name__}: {str(e).splitlines()[0][:200]}") from e
    finally:
        engine.dispose()


def _ident(name: str) -> str:
    """식별자 — 이름은 이 모듈이 [a-z0-9_]로만 만든다. 그래도 따옴표로 감싼다."""
    if not re.fullmatch(r"[a-z0-9_]{1,63}", name):
        raise SharedDbError(f"쓸 수 없는 이름입니다: {name!r}")
    return f'"{name}"'


def _literal(value: str) -> str:
    """값 — DDL(CREATE ROLE … PASSWORD)은 바인딩 인자를 받지 않는다. 그래서 값을 이 모듈이
    만드는 것(token_urlsafe·이름 규칙)으로만 한정하고 확인한 뒤에 싣는다."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", value):
        raise SharedDbError("값에 쓸 수 없는 글자가 있습니다.")
    return f"'{value}'"


def _table(schema: str, table: str) -> str:
    """표 이름은 앱의 마이그레이션이 만든다 — 우리 규칙 밖일 수 있어 따옴표 규칙으로 감싼다."""
    if not table or len(table) > 63 or "\x00" in table:
        raise SharedDbError(f"쓸 수 없는 표 이름입니다: {table!r}")
    return f'{_ident(schema)}."{table.replace(chr(34), chr(34) * 2)}"'


def _exists(kind: str, name: str) -> bool:
    table, column = {"role": ("pg_roles", "rolname"), "db": ("pg_database", "datname")}[kind]
    return bool(_execute(None, f"SELECT 1 FROM {table} WHERE {column} = {_literal(name)}"))


# --- 공유 단위 ---

def unit_reader(unit_id: int) -> str:
    """예전 「공유 단위」 읽기 역할 — 남은 것을 거둘 때만 쓴다(_drop_unit)."""
    return f"unit_{unit_id}_reader"


def db_reader(db_name: str) -> str:
    """그 DB를 고른 부서의 과제 계정이 드는 읽기 역할 — 그 DB에 접속하고 운영 스키마를 읽는다."""
    return f"reader_{db_name}"


def _check_name(name: str) -> str:
    name = str(name or "").strip().lower()
    if not _DB_NAME_RE.match(name):
        raise SharedDbError(f"DB 이름은 영소문자로 시작하는 영소문자·숫자·밑줄 2~41자입니다: {name or '(비어 있음)'}")
    return name


def databases_of(db: Session, org_id: int | None) -> list[str]:
    """부서가 쓰는 DB — 고른 차례대로. 고르지 않았거나 부서가 없는 과제는 기본 DB 하나."""
    if org_id is None:
        return [DATA_DB]
    names = [r.db_name for r in db.execute(select(OrgDatabase).where(
        OrgDatabase.organization_id == org_id).order_by(OrgDatabase.id)).scalars()]
    return names or [DATA_DB]


def _readers_by_project(db: Session) -> dict[int, list[str]]:
    orgs = {p.id: p.organization_id for p in db.execute(select(Project)).scalars()}
    cache: dict[int | None, list[str]] = {}
    out = {}
    for pid, org in orgs.items():
        if org not in cache:
            cache[org] = databases_of(db, org)
        out[pid] = cache[org]
    return out


def project_db(db: Session, project: Project) -> str:
    """과제의 스키마를 둘 DB — 이미 있으면 그곳, 아니면 과제가 고른 것(부서의 DB일 때), 아니면 부서의 기본."""
    names = databases_of(db, project.organization_id)
    have = db.execute(select(DataSchema).where(DataSchema.project_id == project.id)).scalars().first()
    if have is not None:
        return have.db_name
    chosen = db.get(ProjectDatabase, project.id)
    if chosen is not None and chosen.db_name in names:
        return chosen.db_name
    return DATA_DB if DATA_DB in names else names[0]


def choose_db(db: Session, project: Project, name: str, purpose: str = "", by: str = "planning") -> str:
    """과제가 쓸 DB를 고른다(기획 LLM) — 부서의 DB 중에서만, 스키마가 생긴 뒤에는 바꾸지 않는다. 그 DB에 용도
    설명이 아직 없으면 purpose로 적는다(LLM이 부여 — 뒤에 실제 쓰임으로 다시 쓴다)."""
    name = _check_name(name)
    names = databases_of(db, project.organization_id)
    if name not in names:
        raise SharedDbError(f"{name}은 이 과제의 부서가 쓰는 DB가 아닙니다 — 쓸 수 있는 DB: {', '.join(names)}")
    have = db.execute(select(DataSchema).where(DataSchema.project_id == project.id)).scalars().first()
    if have is not None and have.db_name != name:
        raise SharedDbError(f"이 과제의 자료는 이미 {have.db_name}에 있습니다 — DB를 옮기지 않습니다.")
    row = db.get(ProjectDatabase, project.id) or ProjectDatabase(project_id=project.id, db_name=name)
    row.db_name = name
    db.add(row)
    entry = _database_row(db, name)
    if purpose.strip() and not entry.description.strip():
        entry.description, entry.described_by = purpose.strip()[:500], by
        entry.described_at = datetime.now(timezone.utc)
    db.commit()
    return name


def _database_row(db: Session, name: str) -> DataDatabase:
    row = db.get(DataDatabase, name)
    if row is None:
        row = DataDatabase(name=name, description="")
        db.add(row)
        db.flush()
    return row


def set_databases(db: Session, org_id: int, names: list[str]) -> list[dict]:
    """부서가 쓰는 DB(관리자) — 여럿, 차례가 곧 기본(앞의 것). 자기 과제의 스키마가 있는 DB는 뺄 수 없다."""
    if db.get(Organization, org_id) is None:
        raise SharedDbError("그런 부서가 없습니다.")
    clean: list[str] = []
    for n in names:
        n = _check_name(n)
        if n not in clean:
            clean.append(n)
    if not clean:
        raise SharedDbError("DB를 하나 이상 고르세요.")
    used = {r.db_name for r in db.execute(select(DataSchema).join(Project, Project.id == DataSchema.project_id)
                                          .where(Project.organization_id == org_id)).scalars()}
    kept = used - set(clean)
    if kept:
        raise SharedDbError(f"이 부서 과제의 자료가 있는 DB는 뺄 수 없습니다: {', '.join(sorted(kept))}"
                            " — 옮기려면 안내서의 수동 절차(백업→복원)로.")
    for row in db.execute(select(OrgDatabase).where(OrgDatabase.organization_id == org_id)).scalars():
        db.delete(row)
    db.flush()
    for n in clean:
        db.add(OrgDatabase(organization_id=org_id, db_name=n))
        _database_row(db, n)
    db.commit()
    if configured():
        for n in clean:
            ensure_base(n)
        apply_access(db)
    return org_rows(db)


def org_rows(db: Session) -> list[dict]:
    """관리 화면용 — 부서마다 쓰는 DB와 스키마 수."""
    counts: dict[int, int] = {}
    for project in db.execute(select(Project).where(Project.organization_id.is_not(None))).scalars():
        n = db.execute(select(DataSchema.id).where(DataSchema.project_id == project.id)).all()
        counts[project.organization_id] = counts.get(project.organization_id, 0) + len(n)
    return [{"organization_id": o.id, "name": o.name, "databases": databases_of(db, o.id),
             "schemas": counts.get(o.id, 0)}
            for o in sorted(db.execute(select(Organization)).scalars(), key=lambda o: o.name)]


def database_rows(db: Session) -> list[dict]:
    """DB 목록 — 용도 설명(LLM이 적은 것), 쓰는 부서, 스키마 수. 고른 부서가 없고 자료도 없는 DB도 보인다."""
    names = {DATA_DB} | {r.name for r in db.execute(select(DataDatabase)).scalars()} | \
        {r.db_name for r in db.execute(select(OrgDatabase)).scalars()} | set(_db_names(db))
    orgs = org_rows(db)
    schemas: dict[str, int] = {}
    for r in db.execute(select(DataSchema)).scalars():
        schemas[r.db_name] = schemas.get(r.db_name, 0) + 1
    out = []
    for name in sorted(names):
        row = db.get(DataDatabase, name)
        out.append({"name": name, "description": row.description if row else "",
                    "described_by": row.described_by if row else "",
                    "described_at": row.described_at.isoformat() if row and row.described_at else None,
                    "organizations": [o["name"] for o in orgs if name in o["databases"]],
                    "schemas": schemas.get(name, 0)})
    return out


# --- 스키마·계정 ---

def schema_for(project: Project, profile: BuildProfile) -> str:
    """스키마 이름 — 과제 이름(개발은 _dev). 계정은 app_{스키마}."""
    slug = _NAME_RE.sub("_", project.name.lower()).strip("_") or f"p{project.id}"
    return slug[:52] + ("_dev" if profile == BuildProfile.development else "")


def _new_password() -> str:
    return secrets.token_urlsafe(24)


def dsn(row: DataSchema, db_name: str | None = None) -> str:
    """앱에 줄 접속 주소(libpq 형식) — 앱의 언어가 무엇이든 받는 모양이다. 스키마는 검색 경로로 잡힌다.
    db_name을 주면 같은 계정으로 부서의 다른 DB에 접속하는 주소(그 DB의 표를 읽는다)."""
    admin = _admin_url()
    host = get_settings().shared_db_app_host.strip() or admin.host or "127.0.0.1"
    port = admin.port or 5432
    return (f"postgresql://{row.role_name}:{decrypt_value(row.password_encrypted)}"
            f"@{host}:{port}/{db_name or row.db_name}")


def ensure_base(db_name: str) -> None:
    """자료 DB와 그 접속 권한 — 몇 번을 불러도 같다. 접속은 그 DB의 읽기 역할(그 DB를 고른 부서의 과제)만."""
    if not _exists("db", db_name):
        _execute(None, f"CREATE DATABASE {_ident(db_name)}")
    _execute(None, f"REVOKE CONNECT ON DATABASE {_ident(db_name)} FROM PUBLIC")
    if _exists("role", APP_GROUP):   # 예전 구성 — 모든 앱 계정이 기본 DB에 접속했다
        _execute(None, f"REVOKE CONNECT ON DATABASE {_ident(db_name)} FROM {_ident(APP_GROUP)}")
    reader = db_reader(db_name)
    _ensure_role(reader)
    _execute(None, f"GRANT CONNECT ON DATABASE {_ident(db_name)} TO {_ident(reader)}")
    # public 스키마는 쓰지 않는다 — 모든 표는 과제의 스키마에 있다.
    _execute(db_name, "REVOKE ALL ON SCHEMA public FROM PUBLIC")


def _ensure_role(name: str, login_password: str | None = None) -> None:
    if login_password is None:
        if not _exists("role", name):
            _execute(None, f"CREATE ROLE {_ident(name)} NOLOGIN")
        return
    verb = "ALTER" if _exists("role", name) else "CREATE"
    _execute(None, f"{verb} ROLE {_ident(name)} LOGIN PASSWORD {_literal(login_password)}")


def provision(db: Session, project: Project, profile: BuildProfile) -> DataSchema:
    """이 과제·프로필의 스키마와 계정 — 없으면 만들고, 있으면 그대로(몇 번을 불러도 같다).

    배포가 환경변수를 만들 때 부른다(deployer.resolve_env). 비밀번호는 DB에 만들기 **전에**
    저장한다 — 만든 뒤에 저장이 실패하면 아무도 모르는 비밀번호의 계정이 남는다.
    """
    names = databases_of(db, project.organization_id)
    target_db = project_db(db, project)
    row = db.execute(select(DataSchema).where(
        DataSchema.project_id == project.id, DataSchema.profile == profile.value,
    )).scalar_one_or_none()
    if row is None:
        schema = schema_for(project, profile)
        if db.execute(select(DataSchema).where(DataSchema.schema_name == schema)).scalar_one_or_none():
            # 지운 과제의 스키마가 같은 이름으로 남아 있다 — 남의 자료를 물려받지 않게 피한다.
            schema = f"{schema[:44]}_{project.id}"
        row = DataSchema(project_id=project.id, project_name=project.name, profile=profile.value,
                         db_name=target_db, schema_name=schema, role_name=f"app_{schema}",
                         password_encrypted=encrypt_value(_new_password()))
        db.add(row)
        db.commit()
    elif row.db_name not in names:
        raise SharedDbError(
            f"이 과제의 자료는 {row.db_name}에 있는데 과제의 부서가 그 DB를 쓰지 않습니다 — 부서의 DB에 다시 더하거나"
            " 안내서의 수동 절차로 옮기세요.")
    _database_row(db, row.db_name)
    for name in names:
        ensure_base(name)
    settings = get_settings()
    role = _ident(row.role_name)
    schema = _ident(row.schema_name)
    _ensure_role(row.role_name, decrypt_value(row.password_encrypted))
    # 관리 계정이 이 계정이 될 수 있어야 그 소유의 스키마를 만들고, 백업·대조 때 읽는다.
    _execute(None, f"GRANT {role} TO CURRENT_USER")
    # 한 과제가 서버를 붙잡지 않게 — 여러 부서가 같은 서버를 쓴다.
    _execute(None, f"ALTER ROLE {role} CONNECTION LIMIT {int(settings.shared_db_connection_limit)}")
    _execute(None, f"ALTER ROLE {role} SET statement_timeout = '{int(settings.shared_db_statement_timeout)}s'")
    _execute(row.db_name, f"CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION {role}")
    # 표 이름만 쓰면 자기 스키마에 만들어진다 — 앱 코드에 스키마를 박지 않아도 된다.
    _execute(None, f"ALTER ROLE {role} IN DATABASE {_ident(row.db_name)} SET search_path TO {schema}")
    _share(row)
    _apply_readers(row, names, _memberships())
    db.commit()
    return row


def _drop_unit(row: DataSchema) -> None:
    """예전 「공유 단위」 읽기를 거둔다 — 남아 있으면 단위 밖으로 바뀐 부서가 계속 읽는다."""
    if row.unit_id is None:
        return
    role, schema, reader = _ident(row.role_name), _ident(row.schema_name), _ident(unit_reader(row.unit_id))
    _execute(None, f"REVOKE {reader} FROM {role}")
    if row.profile == BuildProfile.release.value:
        _execute(row.db_name, f"ALTER DEFAULT PRIVILEGES FOR ROLE {role} IN SCHEMA {schema}"
                              f" REVOKE SELECT ON TABLES FROM {reader}")
        _execute(row.db_name, f"REVOKE SELECT ON ALL TABLES IN SCHEMA {schema} FROM {reader}")
        _execute(row.db_name, f"REVOKE USAGE ON SCHEMA {schema} FROM {reader}")
    row.unit_id = None


def _share(row: DataSchema) -> None:
    """운영 스키마를 그 DB의 읽기 역할이 들어가 읽게 한다(앞으로 만들 표까지). 개발 스키마는 공유하지 않는다."""
    if row.profile != BuildProfile.release.value:
        return
    role, schema, reader = _ident(row.role_name), _ident(row.schema_name), _ident(db_reader(row.db_name))
    _execute(row.db_name, f"GRANT USAGE ON SCHEMA {schema} TO {reader}")
    _execute(row.db_name, f"ALTER DEFAULT PRIVILEGES FOR ROLE {role} IN SCHEMA {schema}"
                          f" GRANT SELECT ON TABLES TO {reader}")
    _execute(row.db_name, f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {reader}")


def _memberships() -> set[tuple[str, str]]:
    """(읽기 역할, 계정) — 지금 서버에 있는 DB 읽기 역할 소속."""
    return {(r, m) for r, m in _execute(None, (
        "SELECT r.rolname, m.rolname FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid"
        " JOIN pg_roles m ON m.oid = a.member WHERE r.rolname LIKE 'reader\\_%'"))}


def _apply_readers(row: DataSchema, names: list[str], have: set[tuple[str, str]]) -> int:
    """이 계정을 부서가 쓰는 DB들의 읽기 역할에 넣고, 쓰지 않게 된 DB의 읽기 역할에서는 뺀다. 바꾼 수."""
    _drop_unit(row)
    want = {db_reader(n) for n in names}
    mine = {r for r, m in have if m == row.role_name}
    for reader in sorted(want - mine):
        _ensure_role(reader)
        _execute(None, f"GRANT {_ident(reader)} TO {_ident(row.role_name)}")
    for reader in sorted(mine - want):
        _execute(None, f"REVOKE {_ident(reader)} FROM {_ident(row.role_name)}")
    return len(want - mine) + len(mine - want)


def apply_access(db: Session) -> dict:
    """부서가 쓰는 DB가 바뀌었으면 읽기 역할 소속을 따라 바꾼다. 자기 스키마의 DB를 부서가 쓰지 않게 된 과제는
    건드리지 않고 남긴다(blocked) — 다음 배포가 사유와 함께 멈춘다."""
    readers = _readers_by_project(db)
    have = _memberships()
    changed, blocked = 0, []
    for row in db.execute(select(DataSchema).where(DataSchema.project_id.is_not(None))).scalars():
        names = readers.get(row.project_id, [DATA_DB])
        if row.db_name not in names:
            blocked.append(row.schema_name)
            continue
        changed += _apply_readers(row, names, have)
    db.commit()
    return {"changed": changed, "blocked": blocked}


# --- DB 용도 설명(LLM) ---

DESCRIBE_PROMPT = """사내 공용 PostgreSQL 서버의 자료 DB 하나가 **무엇에 쓰이는지** 한두 문장(한국어, 200자 안)으로 적는다.
아래는 그 DB를 쓰는 부서·과제와 들어 있는 표(표 설명 포함)다. 어떤 업무의 어떤 자료가 모이는 DB인지, 새 과제가
이 DB를 골라야 할 때가 언제인지 알 수 있게 쓴다. 지금 설명이 있으면 실제 내용에 맞게 고친다(맞으면 그대로).
설명 문장만 낸다(따옴표·머리말 없이).

DB: {name}
지금 설명: {current}
쓰는 부서: {orgs}
과제: {projects}
표(스키마.표 — 설명):
{tables}"""


def _db_facts(db: Session, name: str) -> dict:
    owners = [r for r in db.execute(select(DataSchema).where(DataSchema.db_name == name)).scalars()
              if r.profile == BuildProfile.release.value]
    tables_here = [(t["schema"], t["table"], t["comment"]) for t in tables(db, name)]
    return {"orgs": sorted(o["name"] for o in org_rows(db) if name in o["databases"]),
            "projects": sorted({r.project_name for r in owners if r.project_name}),
            "tables": tables_here[:200]}


def describe_databases(db: Session) -> list[str]:
    """DB 안의 표·표 설명·쓰는 과제가 지난번 설명 이후 바뀐 DB만 LLM이 용도 설명을 다시 쓴다(기본 프로바이더).
    바뀐 것이 없으면 부르지 않는다. 실패해도 권한 대조는 계속된다 — 다시 쓴 DB 이름을 돌려준다."""
    from . import llm  # noqa: PLC0415 — 순환 import 회피

    provider = llm.default_provider(db)
    if provider is None:
        return []
    done = []
    for name in sorted({r["name"] for r in database_rows(db)}):
        try:
            facts = _db_facts(db, name)
        except SharedDbError:
            continue
        if not facts["tables"] and not facts["projects"]:
            continue   # 아직 쓰임이 없다 — 기획이 고를 때 적은 설명(있으면)을 둔다
        signature = hashlib.sha1(json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        row = _database_row(db, name)
        if row.signature == signature:
            continue
        prompt = DESCRIBE_PROMPT.format(
            name=name, current=row.description or "(없음)", orgs=", ".join(facts["orgs"]) or "(없음)",
            projects=", ".join(facts["projects"]) or "(없음)",
            tables="\n".join(f"- {s}.{t}" + (f" — {c}" if c else "") for s, t, c in facts["tables"]) or "- (없음)")
        try:
            text = llm.chat_completion(provider, [{"role": "user", "content": prompt}], db)
        except Exception:  # noqa: BLE001 — 설명은 덤이다: 다음 대조에 다시 해 본다
            continue
        text = re.sub(r"\s+", " ", str(text or "")).strip().strip('"\'「」').strip()
        if text:
            row.description, row.described_by = text[:500], "llm"
            row.described_at = datetime.now(timezone.utc)
        row.signature = signature
        db.commit()
        done.append(name)
    return done


# --- 표·권한 ---

def _db_names(db: Session) -> list[str]:
    return sorted({r.db_name for r in db.execute(select(DataSchema)).scalars()})


def tables(db: Session, db_name: str | None = None) -> list[dict]:
    """플랫폼이 만든 운영 스키마의 표 — 이름·설명·주인 과제·DB. 행은 읽지 않는다."""
    owners = {r.schema_name: r for r in db.execute(select(DataSchema).where(
        DataSchema.profile == BuildProfile.release.value)).scalars()
        if db_name is None or r.db_name == db_name}
    out = []
    for name in sorted({r.db_name for r in owners.values()}):
        schemas = ", ".join(_literal(s) for s, r in owners.items() if r.db_name == name)
        for schema, table, comment in _execute(name, (
                "SELECT n.nspname, c.relname, obj_description(c.oid, 'pg_class')"
                " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                f" WHERE c.relkind IN ('r', 'p', 'v') AND n.nspname IN ({schemas}) ORDER BY 1, 2")):
            owner = owners.get(schema)
            out.append({"schema": schema, "table": table, "comment": comment or "", "db_name": name,
                        "project_id": owner.project_id if owner else None,
                        "project_name": owner.project_name if owner else ""})
    return out


def _release_row(db: Session, project_id: int) -> DataSchema | None:
    return db.execute(select(DataSchema).where(
        DataSchema.project_id == project_id,
        DataSchema.profile == BuildProfile.release.value)).scalar_one_or_none()


def grant(db: Session, schema: str, table: str, project: Project, privilege: str,
          granted_by: str, expires_at: datetime | None = None) -> DataGrant:
    """다른 과제에 표 하나를 읽기/쓰기로 준다(관리자) — 권한 표에 적고 바로 반영한다.

    받는 과제의 부서가 그 DB를 쓸 때만 줄 수 있다 — 쓰지 않는 DB에는 접속도 못 한다(그 DB를 부서에 더하는 것은
    부서의 DB 설정이다). 같은 DB를 쓰면 읽기는 이미 되므로, 표 단위로 주는 것은 대개 쓰기다.
    """
    if privilege not in PRIVILEGES:
        raise SharedDbError("권한은 read 또는 write입니다.")
    owner = db.execute(select(DataSchema).where(DataSchema.schema_name == schema)).scalar_one_or_none()
    if owner is None or not any(t["schema"] == schema and t["table"] == table
                                for t in tables(db, owner.db_name)):
        raise SharedDbError(f"표가 없습니다: {schema}.{table} — 그 과제가 배포돼 표를 만든 뒤에 줄 수 있습니다.")
    if owner.project_id == project.id:
        raise SharedDbError("자기 과제의 표입니다 — 줄 필요가 없습니다.")
    mine = _release_row(db, project.id)
    if mine is None:
        raise SharedDbError("받을 과제가 아직 공용 DB를 쓰지 않습니다 — 「공용 DB」를 바인딩해 배포한 뒤에 주세요.")
    if owner.db_name not in databases_of(db, project.organization_id):
        raise SharedDbError(f"받을 과제의 부서가 {owner.db_name}을 쓰지 않습니다 — 그 DB의 표는 줄 수 없습니다"
                            "(부서의 DB에 더한 뒤에 주세요).")
    row = db.execute(select(DataGrant).where(
        DataGrant.schema_name == schema, DataGrant.table_name == table,
        DataGrant.project_id == project.id)).scalar_one_or_none()
    if row is None:
        row = DataGrant(schema_name=schema, table_name=table, project_id=project.id)
        db.add(row)
    row.privilege, row.expires_at, row.granted_by = privilege, expires_at, granted_by
    db.commit()
    reconcile(db)
    return row


def revoke(db: Session, grant_id: int) -> None:
    row = db.get(DataGrant, grant_id)
    if row is None:
        raise SharedDbError("그런 권한이 없습니다.")
    db.delete(row)
    db.commit()
    reconcile(db)


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def reconcile(db: Session, now: datetime | None = None) -> dict:
    """PostgreSQL 권한을 플랫폼의 표에 맞춘다 — DB 읽기 역할 소속, 표 권한, 스키마 출입(USAGE). 바뀐 DB는 용도
    설명을 LLM이 다시 쓴다.

    없는 것은 주고, 권한 표에 없는 것·만료된 것은 거둔다. 대상은 플랫폼이 만든 계정이 **남의
    스키마에** 가진 권한이다(자기 스키마의 주인 권한과 DB 읽기 역할은 _share·_apply_readers가 맡는다).
    """
    now = now or datetime.now(timezone.utc)
    expired = [g for g in db.execute(select(DataGrant)).scalars()
               if g.expires_at is not None and _aware(g.expires_at) <= now]
    for g in expired:
        db.delete(g)
    db.commit()
    access = apply_access(db)

    rows = list(db.execute(select(DataSchema)).scalars())
    readers = _readers_by_project(db)
    for r in rows:
        if r.db_name in readers.get(r.project_id, ()) or r.project_id is None:
            _share(r)
    roles_of: dict[int, dict[str, DataSchema]] = {}
    for r in rows:
        if r.project_id is not None:
            roles_of.setdefault(r.project_id, {})[r.profile] = r
    grants = list(db.execute(select(DataGrant)).scalars())
    granted = revoked = 0
    for name in _db_names(db):
        schemas = {r.schema_name: r for r in rows if r.db_name == name}
        app_roles = {r.role_name for r in rows}
        existing = {(t["schema"], t["table"]) for t in tables(db, name)}
        want_table: dict[tuple[str, str, str], set[str]] = {}
        want_usage: set[tuple[str, str]] = set()
        for g in grants:
            if g.schema_name not in schemas or (g.schema_name, g.table_name) not in existing:
                continue  # 다른 DB의 표이거나, 아직 없거나 지워졌다 — 생기면 다음 대조에서 준다
            if name not in readers.get(g.project_id, ()):
                continue   # 받는 과제의 부서가 이 DB를 쓰지 않는다(뒤에 뺐다) — 주지 않는다
            for profile, r in roles_of.get(g.project_id, {}).items():
                # 개발 계정은 남의 표를 읽기만 한다 — 쓰기 권한을 받았어도.
                privs = PRIVILEGES["read" if profile != BuildProfile.release.value else g.privilege]
                want_table.setdefault((r.role_name, g.schema_name, g.table_name), set()).update(privs)
                want_usage.add((r.role_name, g.schema_name))
        if not schemas:
            continue
        names = ", ".join(_literal(s) for s in schemas)
        have_table: dict[tuple[str, str, str], set[str]] = {}
        for grantee, schema, table, priv in _execute(name, (
                "SELECT grantee, table_schema, table_name, privilege_type"
                f" FROM information_schema.role_table_grants WHERE table_schema IN ({names})")):
            if grantee in app_roles and grantee != schemas[schema].role_name:
                have_table.setdefault((grantee, schema, table), set()).add(priv)
        have_usage = {(grantee, schema) for schema, grantee in _execute(name, (
            "SELECT n.nspname, r.rolname FROM pg_namespace n, aclexplode(n.nspacl) a"
            " JOIN pg_roles r ON r.oid = a.grantee"
            f" WHERE a.privilege_type = 'USAGE' AND n.nspname IN ({names})"))
            if grantee in app_roles and grantee != schemas[schema].role_name}

        for key in set(want_table) | set(have_table):
            role, schema, table = key
            want, have = want_table.get(key, set()), have_table.get(key, set())
            if want - have:
                _execute(name, f"GRANT {', '.join(sorted(want - have))} ON {_table(schema, table)} TO {_ident(role)}")
                granted += 1
            extra = (have - want) & set(_TABLE_PRIVILEGES)
            if extra:
                _execute(name, f"REVOKE {', '.join(sorted(extra))} ON {_table(schema, table)} FROM {_ident(role)}")
                revoked += 1
        for role, schema in want_usage - have_usage:
            _execute(name, f"GRANT USAGE ON SCHEMA {_ident(schema)} TO {_ident(role)}")
            granted += 1
        for role, schema in have_usage - want_usage:
            _execute(name, f"REVOKE USAGE ON SCHEMA {_ident(schema)} FROM {_ident(role)}")
            revoked += 1
    try:
        described = describe_databases(db)
    except Exception:  # noqa: BLE001 — 설명은 덤이다
        described = []
    return {"granted": granted, "revoked": revoked, "expired": len(expired),
            "access_changed": access["changed"], "blocked": access["blocked"], "described": described}


def detach_project(db: Session, project_id: int) -> None:
    """과제를 지울 때 — 스키마는 남기고 주인만 비운다(project_name은 남는다). 받은 권한은 지운다."""
    for row in db.execute(select(DataSchema).where(DataSchema.project_id == project_id)).scalars():
        row.project_id = None
    for g in db.execute(select(DataGrant).where(DataGrant.project_id == project_id)).scalars():
        db.delete(g)


def ensure_module(db: Session) -> Module | None:
    """「공용 DB」 모듈 — 설정돼 있으면 하나를 둔다(기획 에이전트의 가용 자원에 보이게)."""
    if not configured():
        return None
    module = db.execute(select(Module).where(Module.name == MODULE_NAME)).scalar_one_or_none()
    if module is None:
        module = Module(name=MODULE_NAME, type=ModuleType.database, category="공용 DB",
                        config={"shared": True})
        db.add(module)
        db.commit()
    return module


def is_shared(module: Module) -> bool:
    return module.type == ModuleType.database and bool((module.config or {}).get("shared"))


def readable(db: Session, project: Project) -> dict[str, list[dict]]:
    """이 과제가 읽을 수 있는 표 — 자기 것(own), 같은 DB를 쓰는 다른 과제의 것(shared), 표 단위로 받은 것(granted).

    부서가 쓰는 DB의 표만 본다 — 쓰지 않는 DB의 표는 이름도 내보내지 않는다.
    """
    out: dict[str, list[dict]] = {"own": [], "shared": [], "granted": []}
    if db.execute(select(DataSchema).where(DataSchema.project_id == project.id)).scalars().first() is None:
        return out
    received = {(g.schema_name, g.table_name): g.privilege for g in db.execute(
        select(DataGrant).where(DataGrant.project_id == project.id)).scalars()}
    for name in databases_of(db, project.organization_id):
        for t in tables(db, name):
            key = (t["schema"], t["table"])
            if t["project_id"] == project.id:
                out["own"].append(t)
            elif key in received:
                out["granted"].append({**t, "privilege": received[key]})
            else:
                out["shared"].append(t)
    return out


def database_choices(db: Session, project: Project) -> list[dict]:
    """기획 LLM이 고를 DB — 부서가 쓰는 DB와 그 용도 설명, 지금 이 과제의 DB인지."""
    current = project_db(db, project)
    descriptions = {r.name: r.description for r in db.execute(select(DataDatabase)).scalars()}
    return [{"name": n, "description": descriptions.get(n, ""), "current": n == current}
            for n in databases_of(db, project.organization_id)]


def schema_summary(db: Session, project: Project) -> str:
    """기획 에이전트에게 — 읽을 수 있는 표는 열까지, 신청하면 쓸 수 있는 표는 이름·설명만.

    관리 계정으로 카탈로그만 읽는다(행은 읽지 않는다). 상한을 넘으면 자른다. 실패해도 기획은
    계속된다 — 사유 한 줄로 대신한다.
    """
    try:
        groups = readable(db, project)
        visible = groups["own"] + groups["shared"] + groups["granted"]
        columns: dict[tuple[str, str], list[str]] = {}
        for name in sorted({t["db_name"] for t in visible}):
            schemas = ", ".join(_literal(s) for s in sorted({t["schema"] for t in visible if t["db_name"] == name}))
            for schema, table, column, kind in _execute(name, (
                    "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns"
                    f" WHERE table_schema IN ({schemas}) ORDER BY table_schema, table_name, ordinal_position")):
                columns.setdefault((schema, table), []).append(f"{column} {kind}")
    except SharedDbError as e:
        return f"- 공용 DB를 읽지 못했습니다({e})"

    def line(t: dict, with_columns: bool) -> str:
        head = f"  - [{t['db_name']}] {t['schema']}.{t['table']}"
        if with_columns:
            head += f"({', '.join(columns.get((t['schema'], t['table']), []))})"
        if t.get("privilege") == "write":
            head += " [쓰기 가능]"
        return head + (f" — {t['comment']}" if t["comment"] else "")

    lines: list[str] = [f"- DB {d['name']}{' (이 과제의 DB)' if d['current'] else ''}: {d['description'] or '(용도 설명 없음)'}"
                        for d in database_choices(db, project)]
    for key, label, with_columns in (("own", "이 앱의 표", True),
                                     ("shared", "같은 DB를 쓰는 과제의 표(읽기)", True),
                                     ("granted", "권한을 받은 표", True)):
        if groups[key]:
            lines.append(f"- {label}:")
            lines += [line(t, with_columns) for t in groups[key]]
    text = "\n".join(lines) or "- 아직 표가 없습니다."
    return text if len(text) <= SCHEMA_SUMMARY_CHARS else text[:SCHEMA_SUMMARY_CHARS] + "\n  - …(이하 생략)"


def backup(db: Session, db_name: str = DATA_DB) -> dict:
    """자료 DB 하나를 pg_dump(-Fc)로 — 날짜 붙은 파일로 남기고 오래된 것은 지운다(최근 N개)."""
    settings = get_settings()
    exe = settings.pg_dump_path.strip() or shutil.which("pg_dump")
    if not exe:
        raise SharedDbError("pg_dump를 찾지 못했습니다 — PAAS_PG_DUMP_PATH를 지정하세요.")
    admin = _admin_url()
    root = Path(settings.shared_db_backup_dir or settings.work_dir / "db-backups") / db_name
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{db_name}-{datetime.now():%Y%m%d-%H%M%S}.dump"
    done = subprocess.run(
        [exe, "-Fc", "-h", admin.host or "127.0.0.1", "-p", str(admin.port or 5432),
         "-U", admin.username or "", "-f", str(target), db_name],
        capture_output=True, timeout=_BACKUP_TIMEOUT, check=False,
        env={**os.environ, "PGPASSWORD": admin.password or ""},
    )
    if done.returncode != 0:
        target.unlink(missing_ok=True)
        detail = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise SharedDbError(f"pg_dump 실패: {detail[-1][:200] if detail else done.returncode}")
    old = sorted(root.glob(f"{db_name}-*.dump"))[:-max(1, settings.shared_db_backup_keep)]
    for path in old:
        path.unlink(missing_ok=True)
    return {"file": target.name, "bytes": target.stat().st_size, "removed": len(old)}


def backup_targets(db: Session) -> list[str]:
    """백업할 DB — 스키마가 있는 자료 DB마다 하나."""
    return _db_names(db)


def listing(db: Session) -> list[dict]:
    """플랫폼이 만든 스키마 목록 — 주인 없는 것(지운 과제의 스키마)도 보인다."""
    rows = db.execute(select(DataSchema).order_by(DataSchema.db_name, DataSchema.schema_name)).scalars()
    return [{"db_name": r.db_name, "schema": r.schema_name, "role": r.role_name, "profile": r.profile,
             "project_id": r.project_id, "project_name": r.project_name,
             "orphan": r.project_id is None, "created_at": r.created_at.isoformat()} for r in rows]
