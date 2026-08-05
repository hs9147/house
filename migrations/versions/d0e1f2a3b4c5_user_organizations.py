"""user organizations (사용자-조직 다대다)

models.UserOrganization이 리비전 없이 추가돼 마이그레이션 이력과 모델이 어긋나 있었다.
기동 시 create_all이 테이블을 만들어 운영에서는 드러나지 않았지만, alembic만으로 스키마를
세우면 이 표가 빠진다. 이미 있는 설치본을 위해 존재하면 건너뛴다.

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-05 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd0e1f2a3b4c5'
down_revision = 'c9d0e1f2a3b4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    if 'user_organizations' in sa.inspect(op.get_bind()).get_table_names():
        return  # create_all로 이미 만들어진 설치본
    op.create_table(
        'user_organizations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user_accounts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_user_organizations_user_id', 'user_organizations', ['user_id'])
    op.create_index(
        'ix_user_organizations_organization_id', 'user_organizations', ['organization_id']
    )


def downgrade() -> None:
    op.drop_index('ix_user_organizations_organization_id', table_name='user_organizations')
    op.drop_index('ix_user_organizations_user_id', table_name='user_organizations')
    op.drop_table('user_organizations')
