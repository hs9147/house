"""temp_job job kind(임시 작업)

Revision ID: a06b7c8d9e0f
Revises: ff5a6b7c8d9e
Create Date: 2026-11-05 15:00:00.000000
"""
from alembic import op


revision = 'a06b7c8d9e0f'
down_revision = 'ff5a6b7c8d9e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return   # SQLite는 sa.Enum을 CHECK 없는 VARCHAR로 만든다 — 파이썬 쪽 JobKind만 늘리면 된다
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobkind ADD VALUE IF NOT EXISTS 'temp_job'")


def downgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute("DELETE FROM scheduled_jobs WHERE kind = 'temp_job'")   # 값은 남는다 — PG ENUM은 값을 뺄 수 없다
