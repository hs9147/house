"""add organizations

Revision ID: 8e63cb096c65
Revises: 3ff8e5f06339
Create Date: 2026-07-15 00:09:21.261328
"""
from alembic import op
import sqlalchemy as sa


revision = '8e63cb096c65'
down_revision = '3ff8e5f06339'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('organizations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_organizations_name'), 'organizations', ['name'], unique=True)
    # SQLite는 ALTER TABLE로 FK 제약을 추가할 수 없어(1차 티어 기본 DB) batch 모드로
    # 처리한다 — Postgres에서는 동일 batch_alter_table이 일반 ALTER로 컴파일된다.
    with op.batch_alter_table('projects') as batch_op:
        batch_op.add_column(sa.Column('organization_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_projects_organization_id', 'organizations', ['organization_id'], ['id']
        )


def downgrade() -> None:
    with op.batch_alter_table('projects') as batch_op:
        batch_op.drop_constraint('fk_projects_organization_id', type_='foreignkey')
        batch_op.drop_column('organization_id')
    op.drop_index(op.f('ix_organizations_name'), table_name='organizations')
    op.drop_table('organizations')
