"""workflow_runs.target_kind·target_name(실행이 진행하는 과제)

Revision ID: d39e0f1a2b3c
Revises: c28d9e0f1a2b
Create Date: 2026-11-12 10:00:00.000000
"""
import sqlalchemy as sa
from alembic import op


revision = 'd39e0f1a2b3c'
down_revision = 'c28d9e0f1a2b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('workflow_runs', sa.Column('target_kind', sa.String(length=64), nullable=False, server_default=''))
    op.add_column('workflow_runs', sa.Column('target_name', sa.String(length=200), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('workflow_runs') as batch:
        batch.drop_column('target_name')
        batch.drop_column('target_kind')
