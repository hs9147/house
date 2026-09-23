"""projects.start_scripts (컴포넌트별 기동 스크립트 본문)

플랫폼의 제네릭 start.cmd는 흔한 모양(package.json·requirements.txt·main.py·app.py·
index.html)만 맞힌다. 맞지 않는 프로젝트는 배포가 실패하거나 엉뚱하게 뜬다 — 실측에서
streamlit이 uvicorn으로 기동을 시도했고, 시그니처가 없는 리포는 정적 서빙으로 흘렀다.

그래서 컴포넌트별 스크립트를 둔다({"": 단일, "api": ..., "web": ...}) — 복합 배포는
컴포넌트마다 유닛·포트·공개 경로가 따로이므로 스크립트도 따로여야 한다. LLM이 리포를 보고
제안하고 사람이 확인해 저장한다. NULL이면 예전처럼 템플릿을 쓴다(기존 동작 그대로).

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-09-23 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = 'a4b5c6d7e8f9'
down_revision = 'f3a4b5c6d7e8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('start_scripts', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'start_scripts')
