"""semantic_tune job kind

Revision ID: fe4f5a6b7c8d
Revises: fd3e4f5a6b7c
Create Date: 2026-10-30 12:00:00.000000
"""
from alembic import op


revision = 'fe4f5a6b7c8d'
down_revision = 'fd3e4f5a6b7c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return   # SQLite는 sa.Enum을 CHECK 없는 VARCHAR로 만든다 — 파이썬 쪽 JobKind만 늘리면 된다
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE jobkind ADD VALUE IF NOT EXISTS 'semantic_tune'")


def downgrade() -> None:
    if op.get_bind().dialect.name != 'postgresql':
        return
    op.execute("DELETE FROM scheduled_jobs WHERE kind = 'semantic_tune'")   # 값은 남는다 — PG ENUM은 값을 뺄 수 없다
