"""source_collect job kind(정보 업데이트 무인 수집)

Revision ID: b17c8d9e0f1a
Revises: a06b7c8d9e0f
Create Date: 2026-11-06 18:00:00.000000
"""
from alembic import op


revision = 'b17c8d9e0f1a'
down_revision = 'a06b7c8d9e0f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return   # SQLite는 sa.Enum을 CHECK 없는 VARCHAR로 만든다 — 파이썬 쪽 JobKind만 늘리면 된다
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobkind ADD VALUE IF NOT EXISTS 'source_collect'")


def downgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute("DELETE FROM scheduled_jobs WHERE kind = 'source_collect'")   # 값은 남는다 — PG ENUM은 값을 뺄 수 없다
