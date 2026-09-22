"""mcp_tokens (외주 개발 에이전트가 쓰는 개인 MCP 토큰)

외주 에이전트에게는 API 키가 없다. 관리자가 키를 나눠 주는 것도 답이 아니다 — 누구에게
나갔는지·언제 회수하는지가 남지 않고, 범위도 좁힐 수 없다. SSO로 로그인한 사람이 자기
몫을 직접 발급하고, 그 사람의 조직 권한으로 프로젝트 접근을 판정한다.

require_api_key는 이 토큰을 받지 않는다 — 개발자 기계의 설정 파일에 놓이는 값이라,
새어도 MCP 밖으로는 아무것도 못 하게 둔다.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-09-22 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = 'f3a4b5c6d7e8'
down_revision = 'e2f3a4b5c6d7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'mcp_tokens',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('label', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('is_admin', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mcp_tokens_token_hash', 'mcp_tokens', ['token_hash'], unique=True)
    op.create_index('ix_mcp_tokens_email', 'mcp_tokens', ['email'])


def downgrade() -> None:
    op.drop_index('ix_mcp_tokens_email', table_name='mcp_tokens')
    op.drop_index('ix_mcp_tokens_token_hash', table_name='mcp_tokens')
    op.drop_table('mcp_tokens')
