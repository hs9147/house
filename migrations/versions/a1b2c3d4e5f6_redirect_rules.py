"""redirect rules

Revision ID: a1b2c3d4e5f6
Revises: 9c605679f0ac
Create Date: 2026-07-15 03:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = '9c605679f0ac'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('redirect_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('from_path', sa.String(length=255), nullable=False),
    sa.Column('to_path', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.Enum('redirect', 'rewrite', name='redirectkind'), nullable=False),
    sa.Column('status_code', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_redirect_rules_project_id'), 'redirect_rules', ['project_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_redirect_rules_project_id'), table_name='redirect_rules')
    op.drop_table('redirect_rules')
