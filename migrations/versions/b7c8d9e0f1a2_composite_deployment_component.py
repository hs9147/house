"""composite deployment component tracking

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-07-15 07:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b7c8d9e0f1a2'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('deployments') as batch_op:
        batch_op.add_column(sa.Column('component', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('deploy_group_id', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('internal_port', sa.Integer(), nullable=True))
        batch_op.create_index(
            op.f('ix_deployments_deploy_group_id'), ['deploy_group_id'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('deployments') as batch_op:
        batch_op.drop_index(op.f('ix_deployments_deploy_group_id'))
        batch_op.drop_column('internal_port')
        batch_op.drop_column('deploy_group_id')
        batch_op.drop_column('component')
