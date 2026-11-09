"""llm_calls.feature·prompt·signals, llm_log_prune job kind(LLM 호출 기록 보완)

Revision ID: c28d9e0f1a2b
Revises: b17c8d9e0f1a
Create Date: 2026-11-07 10:00:00.000000
"""
import sqlalchemy as sa
from alembic import op


revision = 'c28d9e0f1a2b'
down_revision = 'b17c8d9e0f1a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # llm_calls는 리비전 없이 앱이 만들어 온 표다(create_all) — 없으면 리비전을 다 돈 뒤 따라잡기(env._catch_up)가
    # 이 열까지 갖춘 표를 만든다.
    if sa.inspect(op.get_bind()).has_table('llm_calls'):
        _add_columns()
    if op.get_bind().dialect.name == 'postgresql':
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE jobkind ADD VALUE IF NOT EXISTS 'llm_log_prune'")


def _add_columns() -> None:
    op.add_column('llm_calls', sa.Column('feature', sa.String(length=64), nullable=False, server_default=''))
    op.add_column('llm_calls', sa.Column('prompt', sa.String(length=96), nullable=False, server_default=''))
    op.add_column('llm_calls', sa.Column('signals', sa.JSON(), nullable=True))


def downgrade() -> None:
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("DELETE FROM scheduled_jobs WHERE kind = 'llm_log_prune'")
    if not sa.inspect(op.get_bind()).has_table('llm_calls'):
        return
    with op.batch_alter_table('llm_calls') as batch:
        batch.drop_column('signals')
        batch.drop_column('prompt')
        batch.drop_column('feature')
