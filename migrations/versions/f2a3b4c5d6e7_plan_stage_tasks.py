"""plan stage: tasks (작업 지시를 5번째 확정 단계로)

작업 지시가 콘솔 전용 단계에서 정식 확정 단계가 됐다. 산출물은 BuildTask를 렌더한
docs/agent-planning/05-작업지시.md이며, 확정 시 다른 단계와 같은 경로로 커밋된다.

SQLAlchemy 2.x의 Enum은 기본적으로 CHECK 제약을 만들지 않으므로 SQLite에서는 컬럼이
VARCHAR라 손댈 것이 없다. PostgreSQL만 네이티브 ENUM 타입에 값을 추가한다.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-05 00:00:00.000000
"""
from alembic import op


revision = 'f2a3b4c5d6e7'
down_revision = 'e1f2a3b4c5d6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # ALTER TYPE ... ADD VALUE는 같은 트랜잭션 안에서 그 값을 쓸 수 없다 — 별도 커밋으로 뺀다.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE planstage ADD VALUE IF NOT EXISTS 'tasks'")


def downgrade() -> None:
    # PostgreSQL은 ENUM 값 제거를 지원하지 않는다. 남겨 두어도 쓰지 않으면 무해하다.
    pass
