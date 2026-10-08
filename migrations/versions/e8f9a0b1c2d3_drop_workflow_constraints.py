"""workflow_constraints 제거 — 제약은 조직이 아니라 **각 워크플로**가 갖는다

조직 단위 등록부를 뒀다가 걷는다. 업무 규칙은 흐름마다 다르다 — 계약 검토의 선급금 한도는
신규업체등록 워크플로가 지킬 규칙이 아니다. 조직에 묶어 두면 모든 워크플로가 남의 규칙을
받고, 평가 화면에 "이 규칙을 지키는 단계가 없습니다"가 엉뚱한 데서 뜬다. 제약은 그 워크플로가
대화에서 읽어 내 함께 저장한 것(workflows.extracted.constraints)을 쓴다.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-10-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e8f9a0b1c2d3'
down_revision = 'd7e8f9a0b1c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index('ix_workflow_constraints_organization_id',
                  table_name='workflow_constraints')
    op.drop_table('workflow_constraints')


def downgrade() -> None:
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
