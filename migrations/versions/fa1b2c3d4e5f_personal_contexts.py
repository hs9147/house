"""personal_contexts (스마트워크 개인 업무 맥락 — 동의·동기화 폴더·메일 연결)

행이 곧 동의다. 원본은 남기지 않는다 — 변환한 마크다운만 사용자별 숨은 저장소에 두고,
여기에는 바뀐 파일 판정용 원본 크기·수정 시각(files)과 메일 계정·동기화 시각만 둔다.
메일 토큰은 두지 않는다(로그인과 Graph 읽기는 사용자 브라우저가 한다).

Revision ID: fa1b2c3d4e5f
Revises: f9a0b1c2d3e4
Create Date: 2026-10-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'fa1b2c3d4e5f'
down_revision = 'f9a0b1c2d3e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'personal_contexts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('consented_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('folders', sa.JSON(), nullable=True),
        sa.Column('files', sa.JSON(), nullable=True),
        sa.Column('mail_account', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('mail_synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_personal_contexts_email', 'personal_contexts', ['email'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_personal_contexts_email', table_name='personal_contexts')
    op.drop_table('personal_contexts')
