"""공용 DB — 같은 서버의 PostgreSQL에 과제마다 스키마를 주고, 부서 간 경계를 권한으로 막는다.

**구조.** 기본은 자료 DB 하나(paas_data)다 — 함께 쓰기로 한 부서끼리는 한 질의로 서로의 표를 묶어
쓸 수 있어야 하고, 공유 범위를 바꿀 때 데이터를 옮기지 않아도 돼야 한다. 과제마다 스키마를 따로
두는 것은 표 이름과 마이그레이션 관리 표(alembic_version 등)가 과제끼리 부딪히지 않게 하려는 것이다.
스키마 이름은 과제 이름이다(플랫폼에서 유일하고, 조직 이름은 SQL에 쓰기 불편한 글자가 많다).

**공유 단위.** 기본은 자기 부서다. 관리자가 부서의 공유 단위를 다른 부서(상위 부서)로 지정하면 그
부서와 같은 단위가 된다(OrgDataPolicy). **같은 공유 단위의 과제는 서로의 운영 표를 읽는다**(단위
읽기 역할 unit_{id}_reader). 단위 밖의 과제는 그 스키마에 **들어가지도 못한다**(USAGE가 없다) —
관리자가 표 단위로 준 경우(DataGrant)만 예외다. 표 권한이 실수로 새어도 스키마에서 한 번 더 막힌다.

**분리.** 하나의 DB에서는 표 이름·열 이름이 카탈로그로 보인다(데이터는 막혀도). 그것도 보이면 안
되는 공유 단위는 관리자가 「분리」로 지정한다 — 그 단위만 자기 DB(paas_org_{id})를 쓰고, 그 DB에는
그 단위의 계정만 접속한다. 대가로 다른 단위의 표와 묶어 쓸 수 없다.

**자료가 생긴 뒤에는 DB를 옮기지 않는다.** 분리를 켜고 끄거나 분리된 단위로 옮기면 스키마가 다른
DB로 가야 하는데(백업→복원), 되돌리기 어렵다. 그런 설정은 거부하고 안내서의 수동 절차로 둔다.
같은 DB 안의 공유 단위 변경은 권한만 바꾸므로 바로 적용된다.

**권한의 원천은 플랫폼의 표다**(OrgDataPolicy·DataGrant). PostgreSQL 권한은 그 반영이고 reconcile이
주기적으로 맞춘다 — 손으로 준 권한과 만료된 권한은 거둔다. 막는 것은 DB 자체라 앱이 우회할 수 없다.

**관리 계정은 슈퍼유저가 아니다**(CREATEDB·CREATEROLE). 만든 앱 계정은 관리 계정에 GRANT한다 —
그래야 그 계정 소유의 스키마를 만들고, 백업·대조·스키마 요약 때 읽는다.

**지우지 않는다.** 바인딩을 풀어도, 과제를 지워도 스키마는 남는다 — 데이터는 되돌릴 수 없다.
"""
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
    BuildProfile, DataGrant, DataSchema, Module, ModuleType, Organization, OrgDataPolicy, Project,
)
from ..security import decrypt_value, encrypt_value

MODULE_NAME = "shared-db"
DATA_DB = "paas_data"
APP_GROUP = "paas_app"   # 모든 앱 계정이 속한다 — 기본 자료 DB에 접속하는 권한만 준다
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
    "(다른 과제가 그 설명을 보고 쓴다). 개발 배포는 개발 스키마에 쓴다. 같은 공유 단위(부서) 과제의 표와"
    " 권한을 받은 표는 '스키마.표'로 읽는다(쓰기는 권한을 받은 표만). 계정마다 연결 수와 질의 시간에"
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
    return f"unit_{unit_id}_reader"


def _policies(db: Session) -> dict[int, OrgDataPolicy]:
    return {p.organization_id: p for p in db.execute(select(OrgDataPolicy)).scalars()}


def unit_of(db: Session, org_id: int | None, policies: dict[int, OrgDataPolicy] | None = None) -> int | None:
    """이 부서의 공유 단위 — 지정한 부서를 따라간다(그 부서가 또 다른 부서를 지정했으면 거기까지).
    조직이 없는 과제는 단위가 없다(같은 단위 공유 없이, 권한을 받은 표만 읽는다)."""
    if org_id is None:
        return None
    policies = _policies(db) if policies is None else policies
    seen: set[int] = set()
    current = org_id
    while current not in seen:
        seen.add(current)
        policy = policies.get(current)
        if policy is None or policy.share_with_org_id is None:
            return current
        current = policy.share_with_org_id
    raise SharedDbError("공유 단위 설정이 돌고 있습니다(서로를 지정했습니다).")


def db_for_unit(db: Session, unit_id: int | None, policies: dict[int, OrgDataPolicy] | None = None) -> str:
    """공유 단위의 DB — 「분리」로 지정한 단위만 자기 DB를 쓴다."""
    policies = _policies(db) if policies is None else policies
    if unit_id is not None and policies.get(unit_id) is not None and policies[unit_id].isolated:
        return f"paas_org_{unit_id}"
    return DATA_DB


def policy_rows(db: Session) -> list[dict]:
    """관리 화면용 — 부서마다 공유 단위·분리·DB·스키마 수."""
    policies = _policies(db)
    names = {o.id: o.name for o in db.execute(select(Organization)).scalars()}
    counts: dict[int, int] = {}
    for project in db.execute(select(Project).where(Project.organization_id.is_not(None))).scalars():
        n = db.execute(select(DataSchema.id).where(DataSchema.project_id == project.id)).all()
        counts[project.organization_id] = counts.get(project.organization_id, 0) + len(n)
    out = []
    for org_id, name in sorted(names.items(), key=lambda kv: kv[1]):
        policy = policies.get(org_id)
        unit = unit_of(db, org_id, policies)
        out.append({"organization_id": org_id, "name": name,
                    "share_with_org_id": policy.share_with_org_id if policy else None,
                    "isolated": bool(policy and policy.isolated),
                    "unit_id": unit, "unit_name": names.get(unit, ""),
                    "db_name": db_for_unit(db, unit, policies), "schemas": counts.get(org_id, 0)})
    return out


def set_policy(db: Session, org_id: int, share_with_org_id: int | None, isolated: bool) -> list[dict]:
    """부서의 공유 단위·분리 설정(관리자). DB가 바뀌는 부서에 이미 자료가 있으면 거부한다."""
    if db.get(Organization, org_id) is None:
        raise SharedDbError("그런 부서가 없습니다.")
    if share_with_org_id is not None:
        if share_with_org_id == org_id:
            raise SharedDbError("자기 자신을 공유 단위로 지정할 수 없습니다 — 비워 두면 자기 자신입니다.")
        if db.get(Organization, share_with_org_id) is None:
            raise SharedDbError("공유 단위로 지정한 부서가 없습니다.")
        if isolated:
            raise SharedDbError("분리는 공유 단위(다른 부서를 지정하지 않은 부서)에만 켤 수 있습니다.")
    before = _policies(db)
    after = {k: OrgDataPolicy(organization_id=v.organization_id, share_with_org_id=v.share_with_org_id,
                              isolated=v.isolated) for k, v in before.items()}
    after[org_id] = OrgDataPolicy(organization_id=org_id, share_with_org_id=share_with_org_id,
                                  isolated=isolated)
    orgs = [o.id for o in db.execute(select(Organization)).scalars()]
    moved = []
    for oid in orgs:
        old_db = db_for_unit(db, unit_of(db, oid, before), before)
        new_db = db_for_unit(db, unit_of(db, oid, after), after)  # 도는 설정이면 여기서 거부된다
        if old_db != new_db and _org_has_schemas(db, oid):
            moved.append(db.get(Organization, oid).name)
    if moved:
        raise SharedDbError(
            f"자료가 있는 부서의 DB가 바뀌는 설정입니다({', '.join(moved)}) — 스키마를 다른 DB로 옮겨야 해서"
            " 플랫폼이 하지 않습니다. 안내서(infra/postgres/README.md)의 수동 절차를 보세요.")
    row = before.get(org_id)
    if row is None:
        row = OrgDataPolicy(organization_id=org_id)
        db.add(row)
    row.share_with_org_id, row.isolated = share_with_org_id, isolated
    db.commit()
    apply_units(db)
    return policy_rows(db)


def _org_has_schemas(db: Session, org_id: int) -> bool:
    return db.execute(select(DataSchema.id).join(Project, Project.id == DataSchema.project_id)
                      .where(Project.organization_id == org_id).limit(1)).first() is not None


# --- 스키마·계정 ---

def schema_for(project: Project, profile: BuildProfile) -> str:
    """스키마 이름 — 과제 이름(개발은 _dev). 계정은 app_{스키마}."""
    slug = _NAME_RE.sub("_", project.name.lower()).strip("_") or f"p{project.id}"
    return slug[:52] + ("_dev" if profile == BuildProfile.development else "")


def _new_password() -> str:
    return secrets.token_urlsafe(24)


def dsn(row: DataSchema) -> str:
    """앱에 줄 접속 주소(libpq 형식) — 앱의 언어가 무엇이든 받는 모양이다. 스키마는 검색 경로로 잡힌다."""
    admin = _admin_url()
    host = get_settings().shared_db_app_host.strip() or admin.host or "127.0.0.1"
    port = admin.port or 5432
    return (f"postgresql://{row.role_name}:{decrypt_value(row.password_encrypted)}"
            f"@{host}:{port}/{row.db_name}")


def ensure_base(db_name: str, unit_id: int | None) -> None:
    """자료 DB와 그 접속 권한 — 몇 번을 불러도 같다.

    기본 DB에는 모든 앱 계정(paas_app)이 접속한다(스키마에 들어가는 것은 따로 막는다). 분리된 DB에는
    그 공유 단위의 계정만 접속한다 — 그래야 다른 부서에 표 이름도 보이지 않는다.
    """
    if not _exists("role", APP_GROUP):
        _execute(None, f"CREATE ROLE {_ident(APP_GROUP)} NOLOGIN")
    if not _exists("db", db_name):
        _execute(None, f"CREATE DATABASE {_ident(db_name)}")
    _execute(None, f"REVOKE CONNECT ON DATABASE {_ident(db_name)} FROM PUBLIC")
    who = APP_GROUP if db_name == DATA_DB else unit_reader(unit_id) if unit_id is not None else APP_GROUP
    if who != APP_GROUP:
        _ensure_role(who)
    _execute(None, f"GRANT CONNECT ON DATABASE {_ident(db_name)} TO {_ident(who)}")
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
    unit = unit_of(db, project.organization_id)
    target_db = db_for_unit(db, unit)
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
    elif row.db_name != target_db:
        raise SharedDbError(
            f"이 과제의 자료는 {row.db_name}에 있는데 지금 공유 단위의 DB는 {target_db}입니다 — 과제의 부서나"
            " 공유 단위 설정이 바뀌었습니다. 되돌리거나 안내서의 수동 절차로 옮기세요.")
    ensure_base(row.db_name, unit)
    settings = get_settings()
    role = _ident(row.role_name)
    schema = _ident(row.schema_name)
    _ensure_role(row.role_name, decrypt_value(row.password_encrypted))
    # 관리 계정이 이 계정이 될 수 있어야 그 소유의 스키마를 만들고, 백업·대조 때 읽는다.
    _execute(None, f"GRANT {role} TO CURRENT_USER")
    _execute(None, f"GRANT {_ident(APP_GROUP)} TO {role}")
    # 한 과제가 서버를 붙잡지 않게 — 여러 부서가 같은 서버를 쓴다.
    _execute(None, f"ALTER ROLE {role} CONNECTION LIMIT {int(settings.shared_db_connection_limit)}")
    _execute(None, f"ALTER ROLE {role} SET statement_timeout = '{int(settings.shared_db_statement_timeout)}s'")
    _execute(row.db_name, f"CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION {role}")
    # 표 이름만 쓰면 자기 스키마에 만들어진다 — 앱 코드에 스키마를 박지 않아도 된다.
    _execute(None, f"ALTER ROLE {role} IN DATABASE {_ident(row.db_name)} SET search_path TO {schema}")
    _apply_unit(row, unit)
    db.commit()
    return row


def _apply_unit(row: DataSchema, unit: int | None) -> None:
    """같은 공유 단위 읽기 — 이 계정을 단위 읽기 역할에 넣고, 운영 스키마를 그 역할이 들어가 읽게 한다.

    공유 단위가 바뀌었으면 이전 단위의 읽기를 거둔다(이전 단위 과제가 계속 읽으면 안 된다). 개발
    스키마는 공유하지 않는다 — 시험 중인 자료다.
    """
    role = _ident(row.role_name)
    schema = _ident(row.schema_name)
    release = row.profile == BuildProfile.release.value
    old = row.unit_id
    if old is not None and old != unit:
        reader = _ident(unit_reader(old))
        _execute(None, f"REVOKE {reader} FROM {role}")
        if release:
            _execute(row.db_name, f"ALTER DEFAULT PRIVILEGES FOR ROLE {role} IN SCHEMA {schema}"
                                  f" REVOKE SELECT ON TABLES FROM {reader}")
            _execute(row.db_name, f"REVOKE SELECT ON ALL TABLES IN SCHEMA {schema} FROM {reader}")
            _execute(row.db_name, f"REVOKE USAGE ON SCHEMA {schema} FROM {reader}")
    row.unit_id = unit
    if unit is None:
        return
    reader = _ident(unit_reader(unit))
    _ensure_role(unit_reader(unit))
    _execute(None, f"GRANT {reader} TO {role}")
    if release:
        _execute(row.db_name, f"GRANT USAGE ON SCHEMA {schema} TO {reader}")
        _execute(row.db_name, f"ALTER DEFAULT PRIVILEGES FOR ROLE {role} IN SCHEMA {schema}"
                              f" GRANT SELECT ON TABLES TO {reader}")
        _execute(row.db_name, f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {reader}")


def apply_units(db: Session) -> dict:
    """과제의 부서나 공유 단위 설정이 바뀌었으면 읽기 권한을 따라 바꾼다(같은 DB 안에서만).

    DB가 바뀌어야 하는 과제는 건드리지 않고 남긴다(blocked) — 다음 배포가 사유와 함께 멈춘다.
    """
    policies = _policies(db)
    changed, blocked = 0, []
    for row in db.execute(select(DataSchema).where(DataSchema.project_id.is_not(None))).scalars():
        project = db.get(Project, row.project_id)
        unit = unit_of(db, project.organization_id, policies)
        if db_for_unit(db, unit, policies) != row.db_name:
            blocked.append(row.schema_name)
            continue
        if row.unit_id != unit:
            _apply_unit(row, unit)
            changed += 1
    db.commit()
    return {"changed": changed, "blocked": blocked}


# --- 표·권한 ---

def _db_names(db: Session) -> list[str]:
    return sorted({r.db_name for r in db.execute(select(DataSchema)).scalars()})


def tables(db: Session, db_name: str | None = None) -> list[dict]:
    """플랫폼이 만든 운영 스키마의 표 — 이름·설명·주인 과제·공유 단위·DB. 행은 읽지 않는다."""
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
                        "project_name": owner.project_name if owner else "",
                        "unit_id": owner.unit_id if owner else None})
    return out


def _release_row(db: Session, project_id: int) -> DataSchema | None:
    return db.execute(select(DataSchema).where(
        DataSchema.project_id == project_id,
        DataSchema.profile == BuildProfile.release.value)).scalar_one_or_none()


def grant(db: Session, schema: str, table: str, project: Project, privilege: str,
          granted_by: str, expires_at: datetime | None = None) -> DataGrant:
    """다른 과제에 표 하나를 읽기/쓰기로 준다(관리자) — 권한 표에 적고 바로 반영한다.

    같은 DB 안에서만 줄 수 있다 — 분리된 공유 단위의 표는 그 단위 밖으로 나가지 않는다.
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
    if mine.db_name != owner.db_name:
        raise SharedDbError(f"{owner.db_name}의 표는 그 공유 단위 밖으로 줄 수 없습니다(분리된 DB).")
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
    """PostgreSQL 권한을 플랫폼의 표에 맞춘다 — 공유 단위, 표 권한, 스키마 출입(USAGE).

    없는 것은 주고, 권한 표에 없는 것·만료된 것은 거둔다. 대상은 플랫폼이 만든 계정이 **남의
    스키마에** 가진 권한이다(자기 스키마의 주인 권한과 단위 읽기 역할은 _apply_unit이 맡는다).
    """
    now = now or datetime.now(timezone.utc)
    expired = [g for g in db.execute(select(DataGrant)).scalars()
               if g.expires_at is not None and _aware(g.expires_at) <= now]
    for g in expired:
        db.delete(g)
    db.commit()
    units = apply_units(db)

    rows = list(db.execute(select(DataSchema)).scalars())
    roles_of: dict[int, dict[str, DataSchema]] = {}
    for r in rows:
        if r.project_id is not None:
            roles_of.setdefault(r.project_id, {})[r.profile] = r
    grants = list(db.execute(select(DataGrant)).scalars())
    granted = revoked = 0
    for name in _db_names(db):
        schemas = {r.schema_name: r for r in rows if r.db_name == name}
        app_roles = {r.role_name for r in rows if r.db_name == name}
        existing = {(t["schema"], t["table"]) for t in tables(db, name)}
        want_table: dict[tuple[str, str, str], set[str]] = {}
        want_usage: set[tuple[str, str]] = set()
        for g in grants:
            if g.schema_name not in schemas or (g.schema_name, g.table_name) not in existing:
                continue  # 다른 DB의 표이거나, 아직 없거나 지워졌다 — 생기면 다음 대조에서 준다
            for profile, r in roles_of.get(g.project_id, {}).items():
                if r.db_name != name:
                    continue
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
    return {"granted": granted, "revoked": revoked, "expired": len(expired),
            "units_changed": units["changed"], "blocked": units["blocked"]}


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
    """이 과제가 읽을 수 있는 표(자기 것·같은 공유 단위·받은 것)와, 신청하면 쓸 수 있는 표.

    같은 DB의 표만 본다 — 분리된 DB의 표는 이름도 내보내지 않는다.
    """
    out: dict[str, list[dict]] = {"own": [], "unit": [], "granted": [], "requestable": []}
    mine = _release_row(db, project.id) or db.execute(select(DataSchema).where(
        DataSchema.project_id == project.id)).scalars().first()
    if mine is None:
        return out
    unit = unit_of(db, project.organization_id)
    received = {(g.schema_name, g.table_name): g.privilege for g in db.execute(
        select(DataGrant).where(DataGrant.project_id == project.id)).scalars()}
    for t in tables(db, mine.db_name):
        key = (t["schema"], t["table"])
        if t["project_id"] == project.id:
            out["own"].append(t)
        elif unit is not None and t["unit_id"] == unit:
            out["unit"].append(t)
        elif key in received:
            out["granted"].append({**t, "privilege": received[key]})
        else:
            out["requestable"].append(t)
    return out


def schema_summary(db: Session, project: Project) -> str:
    """기획 에이전트에게 — 읽을 수 있는 표는 열까지, 신청하면 쓸 수 있는 표는 이름·설명만.

    관리 계정으로 카탈로그만 읽는다(행은 읽지 않는다). 상한을 넘으면 자른다. 실패해도 기획은
    계속된다 — 사유 한 줄로 대신한다.
    """
    try:
        groups = readable(db, project)
        visible = groups["own"] + groups["unit"] + groups["granted"]
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
        head = f"  - {t['schema']}.{t['table']}"
        if with_columns:
            head += f"({', '.join(columns.get((t['schema'], t['table']), []))})"
        if t.get("privilege") == "write":
            head += " [쓰기 가능]"
        return head + (f" — {t['comment']}" if t["comment"] else "")

    lines: list[str] = []
    for key, label, with_columns in (("own", "이 앱의 표", True),
                                     ("unit", "같은 공유 단위(부서) 과제의 표(읽기)", True),
                                     ("granted", "권한을 받은 표", True),
                                     ("requestable", "다른 공유 단위의 표(관리자에게 신청하면 쓸 수 있다)", False)):
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
    """백업할 DB — 스키마가 있는 자료 DB마다 하나(기본 DB와 분리된 DB들)."""
    return _db_names(db)


def listing(db: Session) -> list[dict]:
    """플랫폼이 만든 스키마 목록 — 주인 없는 것(지운 과제의 스키마)도 보인다."""
    rows = db.execute(select(DataSchema).order_by(DataSchema.db_name, DataSchema.schema_name)).scalars()
    return [{"db_name": r.db_name, "schema": r.schema_name, "role": r.role_name, "profile": r.profile,
             "project_id": r.project_id, "project_name": r.project_name, "unit_id": r.unit_id,
             "orphan": r.project_id is None, "created_at": r.created_at.isoformat()} for r in rows]
