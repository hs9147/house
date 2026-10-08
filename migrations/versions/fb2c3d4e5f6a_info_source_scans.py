"""info_source_scans (스캔 이력 — 다음 스캔의 범위를 정한다) · info_sources.saved_files

스캔 이력은 주소마다 결과와 내용 해시다. saved_files는 지난 저장에서 쓴 파일 목록 — 다음
저장이 같은 자리에 덮어쓰고 이번에 없는 것을 휴지통으로 옮기는 근거다.

Revision ID: fb2c3d4e5f6a
Revises: fa1b2c3d4e5f
Create Date: 2026-10-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'fb2c3d4e5f6a'
down_revision = 'fa1b2c3d4e5f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('info_sources') as batch:
        batch.add_column(sa.Column('saved_files', sa.JSON(), nullable=True))
    op.create_table(
        'info_source_scans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('via', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('pages', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('files', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('changes', sa.JSON(), nullable=True),
        sa.Column('urls', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['source_id'], ['info_sources.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_info_source_scans_source_id', 'info_source_scans', ['source_id'])


def downgrade() -> None:
    op.drop_index('ix_info_source_scans_source_id', table_name='info_source_scans')
    op.drop_table('info_source_scans')
    with op.batch_alter_table('info_sources') as batch:
        batch.drop_column('saved_files')
