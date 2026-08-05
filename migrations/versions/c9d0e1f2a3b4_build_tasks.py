"""build tasks (외주 빌드 작업 지시)

확정된 기획 산출물에서 분해된 작업 지시를 담는다. 산출물(plan_artifacts)이 '무엇을
만들지'를 가리킨다면 이 표는 '어디까지 됐는지'를 담아 외부 빌드의 진행을 추적한다.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-05 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c9d0e1f2a3b4'
down_revision = 'b8c9d0e1f2a3'
branch_labels = None
depends_on = None

STATUS = sa.Enum('pending', 'in_progress', 'done', 'blocked', name='buildtaskstatus')


def upgrade() -> None:
    op.create_table(
        'build_tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('detail', sa.Text(), nullable=False, server_default=''),
        sa.Column('verify', sa.Text(), nullable=False, server_default=''),
        sa.Column('status', STATUS, nullable=False, server_default='pending'),
        sa.Column('note', sa.Text(), nullable=False, server_default=''),
        sa.Column('commit_sha', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id']),
        sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_build_tasks_project_id', 'build_tasks', ['project_id'])
    op.create_index('ix_build_tasks_session_id', 'build_tasks', ['session_id'])


def downgrade() -> None:
    op.drop_index('ix_build_tasks_session_id', table_name='build_tasks')
    op.drop_index('ix_build_tasks_project_id', table_name='build_tasks')
    op.drop_table('build_tasks')
    STATUS.drop(op.get_bind(), checkfirst=True)
