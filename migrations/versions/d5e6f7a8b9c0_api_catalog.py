"""api catalog (외부 API 카탈로그를 DB에 쌓는다)

예전에는 검색이 apis.guru·공공데이터 목록을 통째로 메모리에 캐시하고 그 위에서 걸렀다.
재시작하면 사라지고, 워커가 여럿이면 각자 받고, 검색 경로에 아웃바운드 호출이 섞여
있었다. 수집을 떼어 이 표에 쌓고 검색은 표만 읽는다 — services/apisearch.py 참고.

Revision ID: d5e6f7a8b9c0
Revises: c5d6e7f8a9b0
Create Date: 2026-08-27 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd5e6f7a8b9c0'
down_revision = 'c5d6e7f8a9b0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'api_catalog',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=32), nullable=False),
        sa.Column('ext_id', sa.String(length=255), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('provider', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('categories', sa.JSON(), nullable=False),
        sa.Column('homepage', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('spec_url', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('search_text', sa.Text(), nullable=False, server_default=''),
        # 소스에서 사라진 항목은 지우지 않고 여기에 시각을 찍는다.
        sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'ext_id', name='uq_api_catalog_source_ext'),
    )
    op.create_index('ix_api_catalog_source', 'api_catalog', ['source'])


def downgrade() -> None:
    op.drop_index('ix_api_catalog_source', table_name='api_catalog')
    op.drop_table('api_catalog')
