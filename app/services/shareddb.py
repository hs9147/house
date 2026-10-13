"""공용 DB — 플랫폼과 같은 서버의 PostgreSQL에 앱마다 전용 DB를 만들어 주고, 공용 자료 DB를 둔다.

**왜 database 모듈 하나로 등록하지 않나.** database 모듈은 접속 정보 하나를 바인딩된 모든
프로젝트에 그대로 넣는다(modules.binding_env). 공용 DB를 그렇게 등록하면 모든 앱이 같은 DB에
같은 계정으로 붙어, 한 앱이 다른 앱의 표를 읽고 지울 수 있고 누가 바꿨는지도 가릴 수 없다.
그래서 「공용 DB」 모듈이 바인딩된 프로젝트마다 **DB와 계정을 따로** 만든다 — 운영(release)과
개발(development)도 따로다(개발 중인 코드가 운영 데이터를 지우지 않게).

**공용 자료(paas_common).** 여러 앱이 함께 읽는 기준 정보의 자리. 모든 앱 계정은 common_reader
역할로 **읽기만** 하고, 쓰기는 관리자가 그 자료를 맡긴 프로젝트만 한다(set_common_writer).
그 프로젝트가 만든 표는 기본 권한으로 common_reader가 읽을 수 있게 된다.

**관리 계정은 슈퍼유저가 아니다.** PAAS_SHARED_DB_ADMIN_URL은 CREATEDB·CREATEROLE만 가진다.
만든 앱 계정은 관리 계정에 GRANT한다 — 그래야 그 계정 소유의 DB를 만들고(OWNER), 백업
(pg_dump)과 스키마 요약을 위해 읽을 수 있다.

**지우지 않는다.** 바인딩을 풀어도, 프로젝트를 지워도 DB는 남는다 — 데이터는 되돌릴 수 없다.
"""
import os
import re
import secrets
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import BuildProfile, Module, ModuleType, Project, SharedDatabase
from ..security import decrypt_value, encrypt_value

MODULE_NAME = "shared-db"
COMMON_DB = "paas_common"
COMMON_READER = "common_reader"      # 모든 앱 계정이 속하는 읽기 역할(로그인 없음)
COMMON_LOGIN = "paas_common_reader"  # 플랫폼(DB 조회 MCP)이 공용 자료를 읽는 계정
_NAME_RE = re.compile(r"[^a-z0-9]+")
# 표 수가 많아도 기획 문서의 컨텍스트를 덮지 않게 — 스키마 요약의 상한(글자).
SCHEMA_SUMMARY_CHARS = 4000
_BACKUP_TIMEOUT = 600

# 기획 에이전트가 읽는 사용 규약(available_resources) — 앱을 짓는 쪽이 알아야 하는 사실이다.
USAGE = (
    "이 앱 전용 PostgreSQL DB. 접속 정보는 환경변수 {PREFIX}_DSN(postgresql://…)으로 주입되고,"
    " 운영·개발 배포는 서로 다른 DB를 받는다. 표는 앱이 기동할 때 마이그레이션(Alembic·Prisma·"
    "Flyway 등)으로 만든다. 슈퍼유저 작업·확장 설치·다른 DB 접근은 할 수 없다. 여러 앱이 함께"
    f" 쓰는 기준 정보는 공용 자료 DB '{COMMON_DB}'에 있다 — 같은 계정으로 DSN의 DB 이름만"
    f" '{COMMON_DB}'로 바꿔 접속하면 **읽기만** 된다(쓰기는 관리자가 허락한 앱만)."
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
    (DB 생성·권한 부여는 드물고, 풀을 들고 있으면 DROP·백업을 막는다).
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
    except SharedDbError:
        raise
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


def _exists(kind: str, name: str) -> bool:
    table, column = {"role": ("pg_roles", "rolname"), "db": ("pg_database", "datname")}[kind]
    return bool(_execute(None, f"SELECT 1 FROM {table} WHERE {column} = {_literal(name)}"))


def names_for(project: Project, profile: BuildProfile) -> str:
    """DB 이름 = 계정 이름. app_{프로젝트}(개발은 _dev). 프로젝트 이름은 플랫폼에서 유일하다."""
    slug = _NAME_RE.sub("_", project.name.lower()).strip("_") or f"p{project.id}"
    base = f"app_{slug}"[:58]
    return base + ("_dev" if profile == BuildProfile.development else "")


def _new_password() -> str:
    return secrets.token_urlsafe(24)


def dsn(row: SharedDatabase, dbname: str | None = None) -> str:
    """앱에 줄 접속 주소(libpq 형식) — 앱의 언어가 무엇이든 받는 모양이다."""
    admin = _admin_url()
    host = get_settings().shared_db_app_host.strip() or admin.host or "127.0.0.1"
    port = admin.port or 5432
    return (f"postgresql://{row.role_name}:{decrypt_value(row.password_encrypted)}"
            f"@{host}:{port}/{dbname or row.db_name}")


def ensure_common(db: Session) -> SharedDatabase:
    """공용 자료 DB와 그 읽기 역할·읽기 계정 — 몇 번을 불러도 같다."""
    if not _exists("role", COMMON_READER):
        _execute(None, f"CREATE ROLE {_ident(COMMON_READER)} NOLOGIN")
    row = db.execute(select(SharedDatabase).where(SharedDatabase.profile == "common")).scalar_one_or_none()
    if row is None:
        row = SharedDatabase(project_id=None, project_name="(공용 자료)", profile="common",
                             db_name=COMMON_DB, role_name=COMMON_LOGIN,
                             password_encrypted=encrypt_value(_new_password()))
        db.add(row)
        db.commit()
    _ensure_login(row)
    _execute(None, f"GRANT {_ident(COMMON_READER)} TO {_ident(COMMON_LOGIN)}")
    if not _exists("db", COMMON_DB):
        _execute(None, f"CREATE DATABASE {_ident(COMMON_DB)}")
    _execute(None, f"REVOKE CONNECT ON DATABASE {_ident(COMMON_DB)} FROM PUBLIC")
    _execute(None, f"GRANT CONNECT ON DATABASE {_ident(COMMON_DB)} TO {_ident(COMMON_READER)}")
    _execute(COMMON_DB, f"GRANT USAGE ON SCHEMA public TO {_ident(COMMON_READER)}")
    _execute(COMMON_DB, "REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    return row


def _ensure_login(row: SharedDatabase) -> None:
    """계정을 만들거나 비밀번호를 맞춘다 — 비밀번호는 우리 것이 기준이다(DB가 먼저 있었어도)."""
    password = _literal(decrypt_value(row.password_encrypted))
    if _exists("role", row.role_name):
        _execute(None, f"ALTER ROLE {_ident(row.role_name)} LOGIN PASSWORD {password}")
    else:
        _execute(None, f"CREATE ROLE {_ident(row.role_name)} LOGIN PASSWORD {password}")
    # 관리 계정이 이 계정이 될 수 있어야 그 소유의 DB를 만들고, 백업·스키마 요약 때 읽는다.
    _execute(None, f"GRANT {_ident(row.role_name)} TO CURRENT_USER")


def provision(db: Session, project: Project, profile: BuildProfile) -> SharedDatabase:
    """이 프로젝트·프로필의 DB와 계정 — 없으면 만들고, 있으면 그대로(몇 번을 불러도 같다).

    배포가 환경변수를 만들 때 부른다(deployer.resolve_env). 비밀번호는 DB에 만들기 **전에**
    저장한다 — 만든 뒤에 저장이 실패하면 아무도 모르는 비밀번호의 계정이 남는다.
    """
    row = db.execute(select(SharedDatabase).where(
        SharedDatabase.project_id == project.id, SharedDatabase.profile == profile.value,
    )).scalar_one_or_none()
    if row is None:
        name = names_for(project, profile)
        taken = db.execute(select(SharedDatabase).where(SharedDatabase.db_name == name)).scalar_one_or_none()
        if taken is not None:
            # 지운 프로젝트의 DB가 같은 이름으로 남아 있다 — 남의 데이터를 물려받지 않게 피한다.
            name = f"{name[:50]}_{project.id}"
        row = SharedDatabase(project_id=project.id, project_name=project.name, profile=profile.value,
                             db_name=name, role_name=name,
                             password_encrypted=encrypt_value(_new_password()))
        db.add(row)
        db.commit()
    ensure_common(db)
    _ensure_login(row)
    if not _exists("db", row.db_name):
        _execute(None, f"CREATE DATABASE {_ident(row.db_name)} OWNER {_ident(row.role_name)}")
    _execute(None, f"REVOKE CONNECT ON DATABASE {_ident(row.db_name)} FROM PUBLIC")
    _execute(None, f"GRANT {_ident(COMMON_READER)} TO {_ident(row.role_name)}")
    _apply_common_write(row)
    return row


def _apply_common_write(row: SharedDatabase) -> None:
    role = _ident(row.role_name)
    if row.common_write:
        _execute(COMMON_DB, f"GRANT CREATE ON SCHEMA public TO {role}")
        # 이 계정이 앞으로 만드는 표를 모든 앱이 읽게 한다 — 권한을 표마다 손으로 주지 않는다.
        _execute(COMMON_DB, f"ALTER DEFAULT PRIVILEGES FOR ROLE {role} IN SCHEMA public"
                            f" GRANT SELECT ON TABLES TO {_ident(COMMON_READER)}")
        _execute(COMMON_DB, f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {_ident(COMMON_READER)}")
    else:
        _execute(COMMON_DB, f"REVOKE CREATE ON SCHEMA public FROM {role}")


def set_common_writer(db: Session, project: Project, on: bool) -> list[SharedDatabase]:
    """공용 자료 DB에 쓸 수 있게(또는 다시 읽기만) — 그 프로젝트의 **운영** DB 계정에만 준다.

    개발 계정에 쓰기를 주면 개발 중인 코드가 모든 앱이 읽는 기준 정보를 바꾼다.
    """
    rows = db.execute(select(SharedDatabase).where(
        SharedDatabase.project_id == project.id,
        SharedDatabase.profile == BuildProfile.release.value)).scalars().all()
    if not rows:
        raise SharedDbError("이 프로젝트에는 아직 운영 DB가 없습니다 — 「공용 DB」를 바인딩하고 운영으로"
                            " 배포하면 만들어집니다.")
    for row in rows:
        row.common_write = on
        _apply_common_write(row)
    db.commit()
    return rows


def detach_project(db: Session, project_id: int) -> None:
    """프로젝트를 지울 때 — DB는 남기고 주인만 비운다(project_name은 남는다)."""
    for row in db.execute(select(SharedDatabase).where(SharedDatabase.project_id == project_id)).scalars():
        row.project_id = None


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


def reader_url(db: Session) -> str:
    """공용 자료 DB를 **읽기만** 하는 계정의 SQLAlchemy URL — DB 조회 MCP(/mcp/db/shared-db)가 쓴다."""
    row = ensure_common(db)
    return str(_admin_url().set(username=row.role_name, password=decrypt_value(row.password_encrypted),
                                database=COMMON_DB).render_as_string(hide_password=False))


def schema_summary(db: Session, project: Project | None = None) -> str:
    """공용 자료 DB(와 이 프로젝트의 운영 DB)에 있는 표·열 — 기획 에이전트가 있는 것을 보고 짓게.

    관리 계정으로 information_schema만 읽는다(행은 읽지 않는다). 상한을 넘으면 자른다.
    실패해도 기획은 계속된다 — 사유 한 줄로 대신한다.
    """
    targets = [(f"공용 자료 DB {COMMON_DB}", COMMON_DB)]
    if project is not None:
        row = db.execute(select(SharedDatabase).where(
            SharedDatabase.project_id == project.id,
            SharedDatabase.profile == BuildProfile.release.value)).scalar_one_or_none()
        if row is not None:
            targets.append((f"이 앱의 운영 DB {row.db_name}", row.db_name))
    lines: list[str] = []
    for label, name in targets:
        try:
            rows = _execute(name, "SELECT table_name, column_name, data_type FROM information_schema.columns"
                                  " WHERE table_schema = 'public' ORDER BY table_name, ordinal_position")
        except SharedDbError as e:
            lines.append(f"- {label}: 읽지 못했습니다({e})")
            continue
        tables: dict[str, list[str]] = {}
        for table, column, kind in rows:
            tables.setdefault(table, []).append(f"{column} {kind}")
        if not tables:
            lines.append(f"- {label}: 표 없음")
            continue
        lines.append(f"- {label}:")
        lines += [f"  - {t}({', '.join(cols)})" for t, cols in tables.items()]
    text = "\n".join(lines)
    return text if len(text) <= SCHEMA_SUMMARY_CHARS else text[:SCHEMA_SUMMARY_CHARS] + "\n  - …(이하 생략)"


def backup(db: Session, db_name: str) -> dict:
    """DB 하나를 pg_dump(-Fc)로 — 날짜 붙은 파일로 남기고 오래된 것은 지운다(최근 N개)."""
    settings = get_settings()
    exe = settings.pg_dump_path.strip() or shutil.which("pg_dump")
    if not exe:
        raise SharedDbError("pg_dump를 찾지 못했습니다 — PAAS_PG_DUMP_PATH를 지정하세요.")
    admin = _admin_url()
    root = Path(settings.shared_db_backup_dir or settings.work_dir / "db-backups") / db_name
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{db_name}-{datetime.now():%Y%m%d-%H%M%S}.dump"
    env = {"PGPASSWORD": admin.password or ""}
    done = subprocess.run(
        [exe, "-Fc", "-h", admin.host or "127.0.0.1", "-p", str(admin.port or 5432),
         "-U", admin.username or "", "-f", str(target), db_name],
        capture_output=True, timeout=_BACKUP_TIMEOUT, env={**os.environ, **env}, check=False,
    )
    if done.returncode != 0:
        target.unlink(missing_ok=True)
        detail = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise SharedDbError(f"pg_dump 실패: {detail[-1][:200] if detail else done.returncode}")
    old = sorted(root.glob(f"{db_name}-*.dump"))[:-max(1, settings.shared_db_backup_keep)]
    for path in old:
        path.unlink(missing_ok=True)
    return {"file": target.name, "bytes": target.stat().st_size, "removed": len(old)}


def listing(db: Session) -> list[dict]:
    """플랫폼이 만든 DB 목록 — 주인 없는 것(지운 프로젝트의 DB)도 보인다."""
    rows = db.execute(select(SharedDatabase).order_by(SharedDatabase.profile, SharedDatabase.db_name)).scalars()
    return [{"db_name": r.db_name, "role": r.role_name, "profile": r.profile,
             "project_id": r.project_id, "project_name": r.project_name,
             "orphan": r.project_id is None and r.profile != "common",
             "common_write": r.common_write, "created_at": r.created_at.isoformat()} for r in rows]
