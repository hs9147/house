"""user account approval

가입은 신청일 뿐이고 관리자가 승인해야 로그인할 수 있다 — 도메인만 맞으면 누구나
계정을 만들 수 있던 것을 막는다.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-29 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'user_accounts',
        sa.Column('is_approved', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('user_accounts', 'is_approved')
