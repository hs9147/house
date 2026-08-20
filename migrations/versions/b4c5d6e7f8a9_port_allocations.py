"""port allocations

호스트 포트 배정을 대장으로 관리한다 — 지금까지는 배포 때마다 "지금 아무도 리슨하지
않는 첫 포트"를 골라서, 동시 배포가 같은 포트를 집거나(경쟁) 멈춘 배포의 포트가 남에게
넘어갔다(망각).

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-08-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b4c5d6e7f8a9'
down_revision = 'a3b4c5d6e7f8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'port_allocations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('port', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('profile', sa.Enum('release', 'development', name='buildprofile'), nullable=False),
        sa.Column('component', sa.String(length=16), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'profile', 'component', name='uq_port_owner'),
    )
    op.create_index('ix_port_allocations_port', 'port_allocations', ['port'], unique=True)
    op.create_index('ix_port_allocations_project_id', 'port_allocations', ['project_id'])


def downgrade() -> None:
    op.drop_index('ix_port_allocations_project_id', table_name='port_allocations')
    op.drop_index('ix_port_allocations_port', table_name='port_allocations')
    op.drop_table('port_allocations')
