"""workflow_constraints (조직의 업무 제약 — 기획의 개발 제약과 분리)

기획의 공통 제약사항(plan_constraints)은 **에이전트 개발**에 대한 제한이고, 워크플로의
제약은 업무 규칙이다. 실측에서 섞인 결과가 드러났다: 구매 업무 워크플로 평가에 개발 제약이
실려 "이 규칙을 지키는 단계가 없습니다"가 떴다 — 맞지만 그 워크플로가 지킬 규칙이 아니다.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-10-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd7e8f9a0b1c2'
down_revision = 'c6d7e8f9a0b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'workflow_constraints',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_workflow_constraints_organization_id', 'workflow_constraints',
                    ['organization_id'])


def downgrade() -> None:
    op.drop_index('ix_workflow_constraints_organization_id',
                  table_name='workflow_constraints')
    op.drop_table('workflow_constraints')
