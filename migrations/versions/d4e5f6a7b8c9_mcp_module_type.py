"""mcp module type

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-21 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None

OLD_VALUES = ('external_api', 'internal_api', 'database', 'file_storage')
NEW_VALUES = OLD_VALUES + ('mcp',)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        # SQLite는 sa.Enum 컬럼을 CHECK 제약 없이 VARCHAR로만 만든다(이 리포의 다른
        # 모든 마이그레이션도 동일 — 실제로 임의 문자열 insert가 그대로 통과됨,
        # 확인됨). 그래서 파이썬 쪽 ModuleType만 확장하면 되고 스키마 변경이 없다.
        return
    # PG는 네이티브 ENUM 타입이라 실제로 값을 추가해야 하고, 트랜잭션 밖에서만
    # ADD VALUE가 허용된다.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE moduletype ADD VALUE IF NOT EXISTS 'mcp'")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    count = bind.execute(sa.text("SELECT COUNT(*) FROM modules WHERE type = 'mcp'")).scalar()
    if count:
        raise RuntimeError(
            f"{count}개의 mcp 타입 모듈이 남아 있어 downgrade할 수 없습니다 — 먼저 삭제하세요."
        )
    # PG는 ENUM에서 값을 직접 제거할 수 없다 — 새 타입으로 컬럼을 갈아끼운다.
    op.execute("ALTER TYPE moduletype RENAME TO moduletype_old")
    sa.Enum(*OLD_VALUES, name='moduletype').create(bind)
    op.execute(
        "ALTER TABLE modules ALTER COLUMN type TYPE moduletype USING type::text::moduletype"
    )
    op.execute("DROP TYPE moduletype_old")
