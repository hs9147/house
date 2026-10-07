"""workflows (조직 단위 워크플로 · 구성 대화 · 실행 기록)

워크플로는 조직 단위로 만들고 한 조직이 여러 개를 갖는다(uq_workflow_org_name). 표현은
작은 JSON 스펙이고, 실행 기록에는 **앞 단계 출력까지** 남긴다 — 사람 단계에서 멈춘 실행은
몇 시간 뒤에 이어지므로, 그때 출력이 없으면 LLM 단계를 처음부터 다시 돌려야 한다.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-10-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c6d7e8f9a0b1'
down_revision = 'b5c6d7e8f9a0'
branch_labels = None
depends_on = None

RUN_STATUS = sa.Enum('running', 'waiting', 'succeeded', 'failed', 'canceled',
                     name='workflowrunstatus')


def upgrade() -> None:
    op.create_table(
        'workflows',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('spec', sa.JSON(), nullable=True),
        sa.Column('extracted', sa.JSON(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'name', name='uq_workflow_org_name'),
    )
    op.create_index('ix_workflows_organization_id', 'workflows', ['organization_id'])

    op.create_table(
        'workflow_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('workflow_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_workflow_messages_workflow_id', 'workflow_messages', ['workflow_id'])

    op.create_table(
        'workflow_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('workflow_id', sa.Integer(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('status', RUN_STATUS, nullable=False),
        sa.Column('actor', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('steps', sa.JSON(), nullable=True),
        sa.Column('outputs', sa.JSON(), nullable=True),
        sa.Column('pending_node', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_workflow_runs_workflow_id', 'workflow_runs', ['workflow_id'])


def downgrade() -> None:
    op.drop_index('ix_workflow_runs_workflow_id', table_name='workflow_runs')
    op.drop_table('workflow_runs')
    op.drop_index('ix_workflow_messages_workflow_id', table_name='workflow_messages')
    op.drop_table('workflow_messages')
    op.drop_index('ix_workflows_organization_id', table_name='workflows')
    op.drop_table('workflows')
    RUN_STATUS.drop(op.get_bind(), checkfirst=True)
