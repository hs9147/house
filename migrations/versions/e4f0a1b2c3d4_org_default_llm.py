"""organizations.llm_provider_id(부서별 기본 LLM)

Revision ID: e4f0a1b2c3d4
Revises: d39e0f1a2b3c
Create Date: 2026-11-17 14:00:00.000000
"""
import sqlalchemy as sa
from alembic import op


revision = 'e4f0a1b2c3d4'
down_revision = 'd39e0f1a2b3c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('organizations', sa.Column('llm_provider_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('organizations') as batch:
        batch.drop_column('llm_provider_id')
