"""llm_providers.aws_profile (Bedrock이 쓸 AWS 자격증명 프로필)

Bedrock에는 붙여넣을 정적 키가 없다. 예전에는 api_key를 Bearer로 실어 OpenAI 호환
엔드포인트를 때렸는데, 그건 앞단에 게이트웨이가 있을 때만 통한다 — 네이티브 Bedrock은
AWS 자격증명(SigV4)을 요구한다. 어떤 자격증명을 쓸지는 서버 ~/.aws의 프로필 이름으로
가리킨다(비밀이 아니므로 암호화하지 않는다 — 만료 시 어느 프로필로 재로그인해야
하는지 화면이 말해 줄 수 있어야 한다).

비워 두면 기존 동작(api_key Bearer)을 그대로 유지한다.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
Create Date: 2026-09-21 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op

revision = 'd1e2f3a4b5c6'
down_revision = 'c0d1e2f3a4b5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('llm_providers', sa.Column('aws_profile', sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column('llm_providers', 'aws_profile')
