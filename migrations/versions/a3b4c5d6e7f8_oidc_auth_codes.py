"""oidc auth codes

paas 자체 OIDC Provider(services/oidc_provider.py)가 발급하는 1회용 인증 코드.
UserSession과 같은 이유로 코드 원문이 아니라 sha256 해시만 저장한다.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a3b4c5d6e7f8'
down_revision = 'f2a3b4c5d6e7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'oidc_auth_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('client_id', sa.String(length=128), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('redirect_uri', sa.String(length=512), nullable=False),
        sa.Column('nonce', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code_hash'),
    )
    op.create_index('ix_oidc_auth_codes_code_hash', 'oidc_auth_codes', ['code_hash'])


def downgrade() -> None:
    op.drop_index('ix_oidc_auth_codes_code_hash', table_name='oidc_auth_codes')
    op.drop_table('oidc_auth_codes')
