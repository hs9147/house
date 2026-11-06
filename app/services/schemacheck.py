"""DB 스키마가 코드보다 뒤처졌는지 — 빠진 컬럼을 이름으로 말한다.

**왜 있는가.** `create_all`은 없는 **테이블**만 만들고 기존 테이블에 **컬럼을 추가하지
않는다.** 새 컬럼이 생긴 버전으로 올리고 마이그레이션을 돌리지 않으면 그 테이블을 건드리는
엔드포인트가 전부 500이 되는데, 화면에는 "Internal Server Error"만 남아 원인이 스키마라는
단서가 없다. 같은 일을 세 번 겪었다(projects.structure · chat_sessions.merged_at ·
build_tasks.number) — 기동 로그에만 적으면 로그를 보는 사람만 안다. 그래서 목록을
health에도 실어 콘솔이 띄울 수 있게 한다.
"""
import sqlalchemy as sa

RECOVERY = ("python -m alembic upgrade head — 이미 있는 표·열은 건너뛰고, 리비전에 없던 표·열도 만든다"
            "(migrations/env.py). 「SW 업데이트」가 이것을 돌린다")


_CODE_HEADS: list[str] | None = None


def code_heads() -> list[str]:
    """코드의 마지막 리비전 — 기동할 때 한 번 읽는다(코드는 재시작해야 바뀐다)."""
    global _CODE_HEADS
    if _CODE_HEADS is None:
        try:
            from pathlib import Path  # noqa: PLC0415

            from alembic.config import Config  # noqa: PLC0415
            from alembic.script import ScriptDirectory  # noqa: PLC0415

            root = Path(__file__).resolve().parents[2]
            cfg = Config(str(root / "alembic.ini"))
            cfg.set_main_option("script_location", str(root / "migrations"))
            _CODE_HEADS = sorted(ScriptDirectory.from_config(cfg).get_heads())
        except Exception:  # noqa: BLE001 — 진단이다
            _CODE_HEADS = []
    return _CODE_HEADS


def revision_state(engine) -> dict:
    """DB의 리비전과 코드의 리비전 — 다르면(behind) 마이그레이션이 덜 돌았다. 빠진 열만 보면 막혀 있어도 모른다:
    앱이 표를 만들어 주니 화면은 멀쩡한데, 자료를 옮기는 리비전은 조용히 건너뛰어진 채다(실측)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchall() \
                if sa.inspect(conn).has_table("alembic_version") else []
        db = sorted(r[0] for r in rows)
    except Exception:  # noqa: BLE001
        return {"db": [], "code": code_heads(), "behind": False}
    code = code_heads()
    return {"db": db, "code": code, "behind": bool(code) and db != code}


def missing_columns(engine, metadata) -> list[str]:
    """모델에는 있는데 DB에는 없는 컬럼 이름들(`테이블.컬럼`). 진단이므로 실패는 빈 목록."""
    try:
        inspector = sa.inspect(engine)
        existing = set(inspector.get_table_names())
        missing: list[str] = []
        for table in metadata.sorted_tables:
            if table.name not in existing:
                continue  # create_all이 만들었거나 곧 만든다
            have = {c["name"] for c in inspector.get_columns(table.name)}
            missing += [f"{table.name}.{c.name}" for c in table.columns if c.name not in have]
    except Exception:  # noqa: BLE001 — 진단이 기동을 막으면 본말전도다
        return []
    return missing
