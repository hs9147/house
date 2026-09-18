"""chat_sessions.merged_at (기획 세션 마무리 시각)

'브랜치 머지'와 '진행 현황 업데이트' 중 무엇을 보여줄지 갈리는 값이다. 예전에는 콘솔의
useState로만 들고 있어서 세션을 다시 열면 이미 머지한 세션에 머지 버튼이 또 보였다.

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-09-18 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = 'b9c0d1e2f3a4'
down_revision = 'a8b9c0d1e2f3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('chat_sessions', sa.Column('merged_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('chat_sessions', 'merged_at')
