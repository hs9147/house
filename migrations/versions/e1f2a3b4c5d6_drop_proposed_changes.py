"""drop proposed_changes (내부 에이전트 빌더 제거)

플랫폼 안에서 diff를 만들어 승인·커밋하던 에이전트 빌더가 제거됐다. 구현은 외부
개발도구가 맡고 플랫폼은 기획·제약·검증·모니터링만 한다 — 제안 diff를 담던 표도 함께
내린다. 되돌릴 때를 위해 downgrade로 같은 스키마를 복원한다(내용은 복원되지 않는다).

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-05 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e1f2a3b4c5d6'
down_revision = 'd0e1f2a3b4c5'
branch_labels = None
depends_on = None

STATUS = sa.Enum('proposed', 'applied', 'rejected', name='changestatus')


def upgrade() -> None:
    if 'proposed_changes' not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_table('proposed_changes')
    STATUS.drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    op.create_table(
        'proposed_changes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('diff', sa.Text(), nullable=False),
        sa.Column('summary', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('status', STATUS, nullable=False, server_default='proposed'),
        sa.Column('applied_sha', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['chat_sessions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_proposed_changes_session_id', 'proposed_changes', ['session_id'])
