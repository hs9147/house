"""module category + organization scope

Revision ID: 9c605679f0ac
Revises: 8e63cb096c65
Create Date: 2026-07-15 02:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = '9c605679f0ac'
down_revision = '8e63cb096c65'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('modules') as batch_op:
        batch_op.add_column(sa.Column('category', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('organization_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_modules_organization_id', 'organizations', ['organization_id'], ['id']
        )


def downgrade() -> None:
    with op.batch_alter_table('modules') as batch_op:
        batch_op.drop_constraint('fk_modules_organization_id', type_='foreignkey')
        batch_op.drop_column('organization_id')
        batch_op.drop_column('category')
