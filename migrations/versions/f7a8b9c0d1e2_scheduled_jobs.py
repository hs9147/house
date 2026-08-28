"""scheduled_jobs (주기 갱신 작업과 마지막 실행 결과)

플랫폼이 이미 하고 있던 주기 갱신을 한 곳으로 모은다 — 외부 API 카탈로그 수집은 자기
스레드를 갖고 있었고(24시간 하드코딩), 문서 색인은 스케줄러가 없어 사람이 눌러야 했다.
행은 사람이 만들지 않고 services/scheduler.reconcile()이 지금 있는 것에서 만든다.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-08-28 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f7a8b9c0d1e2'
down_revision = 'e6f7a8b9c0d1'
branch_labels = None
depends_on = None

KIND = sa.Enum('api_catalog', 'doc_index', 'mcp_probe', 'api_probe', name='jobkind')
STATUS = sa.Enum('ok', 'failed', 'skipped', name='jobstatus')


def upgrade() -> None:
    op.create_table(
        'scheduled_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('kind', KIND, nullable=False),
        sa.Column('target', sa.String(length=128), nullable=False, server_default=''),
        sa.Column('interval_seconds', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_status', STATUS, nullable=True),
        sa.Column('last_ms', sa.Integer(), nullable=True),
        sa.Column('last_detail', sa.JSON(), nullable=True),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_scheduled_jobs_name', 'scheduled_jobs', ['name'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_scheduled_jobs_name', table_name='scheduled_jobs')
    op.drop_table('scheduled_jobs')
    STATUS.drop(op.get_bind(), checkfirst=True)
    KIND.drop(op.get_bind(), checkfirst=True)
