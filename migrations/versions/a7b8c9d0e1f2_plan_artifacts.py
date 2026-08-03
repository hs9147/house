"""plan artifacts (에이전트 기획 단계 산출물 포인터)

산출물 본문은 프로젝트 Gitea 리포에 커밋되고, 여기엔 위치(repo_path)·커밋(commit_sha)·
확정(confirmed) 상태만 둔다. 기존 chat_sessions를 기획 세션으로 재사용한다.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-03 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a7b8c9d0e1f2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None

plan_stage = sa.Enum('spec', 'architecture', 'solution', 'principles', name='planstage')


def upgrade() -> None:
    op.create_table(
        'plan_artifacts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('session_id', sa.Integer(), sa.ForeignKey('chat_sessions.id'), nullable=False, index=True),
        sa.Column('stage', plan_stage, nullable=False),
        sa.Column('repo_path', sa.String(length=255), nullable=False),
        sa.Column('commit_sha', sa.String(length=40), nullable=True),
        sa.Column('confirmed', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('session_id', 'stage', name='uq_plan_artifact_session_stage'),
    )


def downgrade() -> None:
    op.drop_table('plan_artifacts')
    plan_stage.drop(op.get_bind(), checkfirst=True)
