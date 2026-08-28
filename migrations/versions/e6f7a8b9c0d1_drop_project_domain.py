"""drop projects.domain (프로젝트별 커스텀 도메인 제거)

입력만 받고 버리는 필드였다. small tier에서 domain_for는 이 값을 보지 않고 늘
base_domain을 돌려줬고, 세 프록시 백엔드의 "전용 사이트" 분기는 그래서 한 번도
실행되지 않았다 — 배포 URL은 서브패스로 통일한다(services/proxy/__init__.py).

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-28 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'e6f7a8b9c0d1'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('projects', 'domain')


def downgrade() -> None:
    # 컬럼은 되돌아오지만 값은 돌아오지 않는다 — 쓰이지 않던 값이라 잃는 것도 없다.
    op.add_column('projects', sa.Column('domain', sa.String(length=255), nullable=True))
