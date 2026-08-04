"""llm provider organization scope

Module과 동일한 조직 범위 규칙을 LlmProvider에도 적용한다 — 미지정(NULL)=전역
(모든 프로젝트에서 사용 가능), 지정 시 해당 조직 소속 프로젝트에서만 사용 가능.
기존 admin 전용 외부 프로바이더 제한을 대체한다.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-04 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'b8c9d0e1f2a3'
down_revision = 'a7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('llm_providers') as batch_op:
        batch_op.add_column(sa.Column('organization_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_llm_providers_organization_id', 'organizations', ['organization_id'], ['id']
        )


def downgrade() -> None:
    with op.batch_alter_table('llm_providers') as batch_op:
        batch_op.drop_constraint('fk_llm_providers_organization_id', type_='foreignkey')
        batch_op.drop_column('organization_id')
