"""build_tasks.number (프로젝트별 작업 번호, 1부터)

커밋 규약(`task #3`)과 화면의 번호가 전역 id였다. 다른 프로젝트에서 작업을 만들 때마다
번호가 밀려서, 새 프로젝트의 첫 작업이 `task #57`로 시작한다 — 사람이 커밋 메시지에 적는
번호인데 프로젝트 안에서 아무 의미가 없는 값이었다.

기존 행은 프로젝트별로 id 순서(= 생성 순서)대로 1부터 채운다.

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-09-21 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = 'e2f3a4b5c6d7'
down_revision = 'd1e2f3a4b5c6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('build_tasks',
                  sa.Column('number', sa.Integer(), nullable=False, server_default='0'))
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, project_id FROM build_tasks ORDER BY project_id, id")
    ).fetchall()
    counters: dict[int, int] = {}
    for task_id, project_id in rows:
        counters[project_id] = counters.get(project_id, 0) + 1
        conn.execute(sa.text("UPDATE build_tasks SET number = :n WHERE id = :i"),
                     {"n": counters[project_id], "i": task_id})


def downgrade() -> None:
    op.drop_column('build_tasks', 'number')
