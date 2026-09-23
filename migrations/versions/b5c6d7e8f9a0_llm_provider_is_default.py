"""llm_providers.is_default (사람이 고르지 않아도 도는 기능이 쓸 기본 모델)

배포 점검·실패 원인 분석·레포 검토처럼 자동으로 도는 판단에는 "어느 모델로?"를 물을 자리가
없다. 물으면 화면마다 선택 상자가 하나 늘고, 정작 기본값은 아무도 정하지 않는다. 그래서
프로바이더 하나를 기본값으로 둔다(하나만 참 — services/llm.set_default가 나머지를 내린다).

기존 설치본에는 기본값이 없다. 프로바이더가 하나뿐이면 그것을 기본값으로 올린다 — 하나뿐인
설치본에서 "기본값이 없어서 못 돈다"는 말은 설명이 아니라 실수다. 둘 이상이면 고르지 않는다:
어느 것으로 돈지는 사람이 정할 일이다.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-09-23 08:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = 'b5c6d7e8f9a0'
down_revision = 'a4b5c6d7e8f9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('llm_providers',
                  sa.Column('is_default', sa.Boolean(), nullable=False,
                            server_default=sa.false()))
    # 프로바이더가 하나뿐이면 그것이 기본값이다.
    op.execute(
        "UPDATE llm_providers SET is_default = 1 "
        "WHERE (SELECT COUNT(*) FROM llm_providers) = 1"
    )


def downgrade() -> None:
    op.drop_column('llm_providers', 'is_default')
