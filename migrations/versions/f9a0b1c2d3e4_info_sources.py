"""info_sources (정보 출처 — 웹사이트·API·MCP를 스캔해 저장소로 옮긴다)

요청 헤더(쿠키·토큰)는 암호화한 JSON 한 칸이다. 스캔 결과와 저장 제안은 행에 둔다 —
저장은 admin이 결정하는 별도 단계라 그 사이에 결과를 다시 만들 이유가 없다.

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-10-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f9a0b1c2d3e4'
down_revision = 'e8f9a0b1c2d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'info_sources',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('url', sa.String(length=1024), nullable=False),
        sa.Column('headers_encrypted', sa.Text(), nullable=True),
        sa.Column('note', sa.Text(), nullable=False, server_default=''),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='new'),
        sa.Column('scan', sa.JSON(), nullable=True),
        sa.Column('proposal', sa.JSON(), nullable=True),
        sa.Column('target_store', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('scanned_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('saved_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )


def downgrade() -> None:
    op.drop_table('info_sources')
