"""DB 스키마가 코드보다 뒤처졌는지 — 빠진 컬럼을 이름으로 말한다.

**왜 있는가.** `create_all`은 없는 **테이블**만 만들고 기존 테이블에 **컬럼을 추가하지
않는다.** 새 컬럼이 생긴 버전으로 올리고 마이그레이션을 돌리지 않으면 그 테이블을 건드리는
엔드포인트가 전부 500이 되는데, 화면에는 "Internal Server Error"만 남아 원인이 스키마라는
단서가 없다. 같은 일을 세 번 겪었다(projects.structure · chat_sessions.merged_at ·
build_tasks.number) — 기동 로그에만 적으면 로그를 보는 사람만 안다. 그래서 목록을
health에도 실어 콘솔이 띄울 수 있게 한다.
"""
import sqlalchemy as sa

RECOVERY = ("alembic upgrade head "
            "(테이블이 이미 있어 실패하면 빠진 컬럼을 ALTER TABLE로 추가하고 alembic stamp head)")


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
