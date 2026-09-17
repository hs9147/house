"""plan_constraints (모든 프로젝트에 적용되는 공통 기획 제약사항)

프로젝트마다 다시 말해 줄 수 없는 환경 제약을 한 곳에 모아 두고, 기획 각 단계의 제약
문서에 실어 준다. 처음 두 행은 지금 운영 환경의 제약을 그대로 넣어 둔다 — 화면에서
삭제·추가할 수 있다.

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-09-16 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a8b9c0d1e2f3'
down_revision = 'f7a8b9c0d1e2'
branch_labels = None
depends_on = None

SEED = [
    "서버가 80포트만 사용 가능해 서브패스로 URL을 만들고 IIS에서 URL rewrite되는 구조다. "
    "AI 호출 API 경로가 중간에 소실되어 서버까지 전달되지 않는다 — API route 대신 내부 함수로 "
    "호출하거나 rewrite를 고려한 상대 주소로 적용해야 한다.",
    "기업 내부 에이전트이므로 외부 솔루션은 사용하지 않는다(Redis 등 불필요).",
]


def upgrade() -> None:
    op.create_table(
        'plan_constraints',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    for text in SEED:
        op.execute(
            sa.text("INSERT INTO plan_constraints (text, created_at) "
                    "VALUES (:text, CURRENT_TIMESTAMP)").bindparams(text=text)
        )


def downgrade() -> None:
    op.drop_table('plan_constraints')
