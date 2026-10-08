"""smartwork_sessions (스마트워크 세션 = 업무 하나 — 맥락·참여자·대화)

맥락(조직·워크플로)은 세션에, 참여자별 개인 맥락 선택(폴더·메일)은 참여자 행에 둔다.
대화는 행 단위다 — 공유 참여자가 동시에 보내도 서로의 메시지를 덮어쓰지 않는다.

Revision ID: fc3d4e5f6a7b
Revises: fb2c3d4e5f6a
Create Date: 2026-10-08 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'fc3d4e5f6a7b'
down_revision = 'fb2c3d4e5f6a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'smartwork_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner', sa.String(length=255), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('organization_id', sa.Integer(), nullable=True),
        sa.Column('workflow_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['workflow_id'], ['workflows.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_smartwork_sessions_owner', 'smartwork_sessions', ['owner'])
    op.create_table(
        'smartwork_session_members',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('folders', sa.JSON(), nullable=True),
        sa.Column('mail', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('added_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['smartwork_sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('session_id', 'email', name='uq_smartwork_session_member'),
    )
    op.create_index('ix_smartwork_session_members_session_id', 'smartwork_session_members',
                    ['session_id'])
    op.create_index('ix_smartwork_session_members_email', 'smartwork_session_members', ['email'])
    op.create_table(
        'smartwork_session_messages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('author', sa.String(length=255), nullable=False, server_default=''),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('view', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['session_id'], ['smartwork_sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_smartwork_session_messages_session_id', 'smartwork_session_messages',
                    ['session_id'])


def downgrade() -> None:
    op.drop_index('ix_smartwork_session_messages_session_id',
                  table_name='smartwork_session_messages')
    op.drop_table('smartwork_session_messages')
    op.drop_index('ix_smartwork_session_members_email', table_name='smartwork_session_members')
    op.drop_index('ix_smartwork_session_members_session_id',
                  table_name='smartwork_session_members')
    op.drop_table('smartwork_session_members')
    op.drop_index('ix_smartwork_sessions_owner', table_name='smartwork_sessions')
    op.drop_table('smartwork_sessions')
