"""mcp_oauth_clients·mcp_oauth_codes·mcp_oauth_grants (GPAX 계정 로그인으로 MCP 연결)

개인 MCP 토큰을 설정 파일에 붙여 넣는 대신, MCP 클라이언트가 GPAX 로그인 창을 열어 사람이
승인하게 한다(OAuth 2.1 + PKCE, 동적 클라이언트 등록). Gitea SSO와 같은 로그인·서명 키를
쓰지만 표는 따로 둔다 — 한쪽 흐름을 고치다 다른 쪽이 깨지지 않게.

Revision ID: a1c2e3f4b5d6
Revises: f5a1b2c3d4e6
Create Date: 2026-11-26 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = 'a1c2e3f4b5d6'
down_revision = 'f5a1b2c3d4e6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'mcp_oauth_clients',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.String(length=64), nullable=False),
        sa.Column('client_name', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('redirect_uris', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mcp_oauth_clients_client_id', 'mcp_oauth_clients', ['client_id'],
                    unique=True)
    op.create_table(
        'mcp_oauth_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('client_id', sa.String(length=64), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('redirect_uri', sa.String(length=512), nullable=False),
        sa.Column('code_challenge', sa.String(length=128), nullable=False),
        sa.Column('resource', sa.String(length=512), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mcp_oauth_codes_code_hash', 'mcp_oauth_codes', ['code_hash'], unique=True)
    op.create_table(
        'mcp_oauth_grants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.String(length=64), nullable=False),
        sa.Column('client_name', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('resource', sa.String(length=512), nullable=False, server_default=''),
        sa.Column('refresh_hash', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_mcp_oauth_grants_client_id', 'mcp_oauth_grants', ['client_id'])
    op.create_index('ix_mcp_oauth_grants_email', 'mcp_oauth_grants', ['email'])
    op.create_index('ix_mcp_oauth_grants_refresh_hash', 'mcp_oauth_grants', ['refresh_hash'],
                    unique=True)


def downgrade() -> None:
    op.drop_index('ix_mcp_oauth_grants_refresh_hash', table_name='mcp_oauth_grants')
    op.drop_index('ix_mcp_oauth_grants_email', table_name='mcp_oauth_grants')
    op.drop_index('ix_mcp_oauth_grants_client_id', table_name='mcp_oauth_grants')
    op.drop_table('mcp_oauth_grants')
    op.drop_index('ix_mcp_oauth_codes_code_hash', table_name='mcp_oauth_codes')
    op.drop_table('mcp_oauth_codes')
    op.drop_index('ix_mcp_oauth_clients_client_id', table_name='mcp_oauth_clients')
    op.drop_table('mcp_oauth_clients')
