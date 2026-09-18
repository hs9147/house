"""projects.structure (리포에서 감지한 배포 단위 목록)

ProjectType 하나로는 백엔드+프론트엔드처럼 서로 다른 Dockerfile 템플릿·다른 내부 포트로
빌드돼야 하는 구성을 표현할 수 없다. 예전에는 `composite` enum 값을 하나 두고 배포할
때마다 backend/·frontend/ 폴더 이름을 다시 확인했다 — 이름이 다르거나 컴포넌트가 셋이면
잡히지 않았고 구성 내용이 어디에도 남지 않았다.

type 컬럼은 이 구조의 대표값으로 그대로 남는다(하위 호환).

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
Create Date: 2026-09-18 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = 'c0d1e2f3a4b5'
down_revision = 'b9c0d1e2f3a4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('structure', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'structure')
