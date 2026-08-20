"""drop file_storage module type

저장소는 더 이상 모듈로 등록하지 않는다 — 경로는 PAAS_STORAGE_ROOT ·
PAAS_DOC_ROOTS 환경변수가 정하고, 접근은 /storage 창구와 사내 MCP 서버
(/mcp/docs, /mcp/storage/{저장소})가 맡는다. services/storage.py 참고.

**이 마이그레이션은 file_storage 모듈 행과 그 바인딩을 지운다.** 파이썬 쪽
ModuleType에서 값이 사라지므로 남아 있으면 모듈 목록 조회 자체가 깨진다(SQLAlchemy
Enum이 알 수 없는 값에서 LookupError를 낸다). 디스크의 폴더는 건드리지 않으므로,
붙여 두었던 폴더는 PAAS_DOC_ROOTS에 경로를 적어 그대로 다시 쓸 수 있다.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-08-20 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c5d6e7f8a9b0'
down_revision = 'b4c5d6e7f8a9'
branch_labels = None
depends_on = None

NEW_VALUES = ('external_api', 'internal_api', 'database', 'mcp', 'llm')


def upgrade() -> None:
    bind = op.get_bind()

    ids = [row[0] for row in bind.execute(
        sa.text("SELECT id FROM modules WHERE type = 'file_storage'")
    )]
    if ids:
        # 바인딩을 먼저 지운다 — module_id 외래키가 걸려 있다.
        bind.execute(
            sa.text("DELETE FROM module_bindings WHERE module_id IN "
                    "(SELECT id FROM modules WHERE type = 'file_storage')")
        )
        bind.execute(sa.text("DELETE FROM modules WHERE type = 'file_storage'"))
        print(f"[c5d6e7f8a9b0] file_storage 모듈 {len(ids)}건과 그 바인딩을 삭제했습니다 "
              f"— 폴더는 그대로이니 PAAS_DOC_ROOTS에 경로를 등록하세요.")

    if bind.dialect.name != 'postgresql':
        # SQLite는 sa.Enum 컬럼을 CHECK 제약 없이 VARCHAR로 만든다(d4e5f6a7b8c9 참고)
        # — 파이썬 쪽 ModuleType만 줄이면 되고 스키마 변경이 없다.
        return
    # PG는 네이티브 ENUM이라 값을 뺀 새 타입으로 컬럼을 갈아끼운다.
    op.execute("ALTER TYPE moduletype RENAME TO moduletype_old")
    sa.Enum(*NEW_VALUES, name='moduletype').create(bind)
    op.execute(
        "ALTER TABLE modules ALTER COLUMN type TYPE moduletype USING type::text::moduletype"
    )
    op.execute("DROP TYPE moduletype_old")


def downgrade() -> None:
    """타입 값만 되돌린다 — 지워진 모듈 행은 복구할 수 없다."""
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE moduletype ADD VALUE IF NOT EXISTS 'file_storage'")
