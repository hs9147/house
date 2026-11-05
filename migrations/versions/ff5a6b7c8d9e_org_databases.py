"""부서별 자료 DB(org_databases)·DB 용도 설명(data_databases)·과제가 고른 DB(project_databases) — 공유 단위 폐지

Revision ID: ff5a6b7c8d9e
Revises: fe4f5a6b7c8d
Create Date: 2026-11-05 12:00:00.000000

공유 단위(org_data_policies)를 없앤다. 「분리」한 단위가 있었으면 그 DB(paas_org_{단위})를 그 단위에 든 부서들이
쓰는 DB로 옮겨 적는다 — 자료는 옮기지 않는다. 나머지 부서는 기본 DB(paas_data)를 쓴다(행이 없으면 기본).
"""
from alembic import op
import sqlalchemy as sa


revision = 'ff5a6b7c8d9e'
down_revision = 'fe4f5a6b7c8d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    present = set(sa.inspect(bind).get_table_names())
    if "data_databases" not in present:
        op.create_table(
            "data_databases",
            sa.Column("name", sa.String(length=63), primary_key=True),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("described_by", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("described_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("signature", sa.String(length=64), nullable=False, server_default=""),
        )
    if "org_databases" not in present:
        op.create_table(
            "org_databases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("db_name", sa.String(length=63), nullable=False),
            sa.UniqueConstraint("organization_id", "db_name", name="uq_org_database"),
        )
        op.create_index("ix_org_databases_organization_id", "org_databases", ["organization_id"])
    if "project_databases" not in present:
        op.create_table(
            "project_databases",
            sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("db_name", sa.String(length=63), nullable=False),
        )

    if "org_data_policies" in present:
        rows = bind.execute(sa.text(
            "SELECT organization_id, share_with_org_id, isolated FROM org_data_policies")).fetchall()
        policy = {r[0]: (r[1], bool(r[2])) for r in rows}

        def unit_of(org: int) -> int:
            seen = set()
            while org not in seen:
                seen.add(org)
                share = policy.get(org, (None, False))[0]
                if share is None:
                    return org
                org = share
            return org

        orgs = [r[0] for r in bind.execute(sa.text("SELECT id FROM organizations")).fetchall()]
        for org in orgs:
            unit = unit_of(org)
            if policy.get(unit, (None, False))[1]:
                name = f"paas_org_{unit}"
                bind.execute(sa.text("INSERT INTO org_databases (organization_id, db_name) VALUES (:o, :n)"),
                             {"o": org, "n": name})
                if not bind.execute(sa.text("SELECT 1 FROM data_databases WHERE name = :n"), {"n": name}).first():
                    bind.execute(sa.text("INSERT INTO data_databases (name, description, described_by, signature)"
                                         " VALUES (:n, '', '', '')"), {"n": name})
        op.drop_table("org_data_policies")


def downgrade() -> None:
    op.create_table(
        "org_data_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("share_with_org_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("isolated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.drop_table("project_databases")
    op.drop_index("ix_org_databases_organization_id", table_name="org_databases")
    op.drop_table("org_databases")
    op.drop_table("data_databases")
