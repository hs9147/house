"""projects.auto_deploy_release·auto_deploy_development(프로필별 자동배포)

기본값: development 켬, release 끔 — 이미 있는 프로젝트도 이 값으로 시작한다(예전에는 웹훅이 기본 프로필,
대개 release를 배포했다). 운영본을 계속 자동배포하려면 프로젝트 「실행 상태」에서 release를 켠다.

Revision ID: f5a1b2c3d4e6
Revises: e4f0a1b2c3d4
Create Date: 2026-11-17 16:00:00.000000
"""
import sqlalchemy as sa
from alembic import op


revision = 'f5a1b2c3d4e6'
down_revision = 'e4f0a1b2c3d4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('auto_deploy_release', sa.Boolean(), nullable=False,
                                        server_default=sa.false()))
    op.add_column('projects', sa.Column('auto_deploy_development', sa.Boolean(), nullable=False,
                                        server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table('projects') as batch:
        batch.drop_column('auto_deploy_development')
        batch.drop_column('auto_deploy_release')
