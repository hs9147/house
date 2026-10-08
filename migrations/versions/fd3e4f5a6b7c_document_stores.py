"""Department-owned document store registry.

Revision ID: fd3e4f5a6b7c
Revises: fc3d4e5f6a7b
"""
from alembic import op
import sqlalchemy as sa


revision = "fd3e4f5a6b7c"
down_revision = "fc3d4e5f6a7b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 앱의 create_all이 먼저 표를 만든 환경에서도 리비전을 적용할 수 있게 한다.
    present = set(sa.inspect(op.get_bind()).get_table_names())
    if "document_stores" not in present:
        op.create_table(
            "document_stores",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=41), nullable=False),
            sa.Column("root_path", sa.String(length=2048), nullable=False),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
            sa.Column("read_only", sa.Boolean(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_document_stores_name", "document_stores", ["name"], unique=True)
        op.create_index("ix_document_stores_organization_id", "document_stores", ["organization_id"])
    if "document_store_registry" not in present:
        op.create_table(
            "document_store_registry",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("document_store_registry")
    op.drop_index("ix_document_stores_organization_id", table_name="document_stores")
    op.drop_index("ix_document_stores_name", table_name="document_stores")
    op.drop_table("document_stores")
