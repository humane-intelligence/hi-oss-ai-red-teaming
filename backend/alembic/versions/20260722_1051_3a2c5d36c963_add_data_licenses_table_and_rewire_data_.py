"""add data_licenses table and rewire data_license to fk

Revision ID: 3a2c5d36c963
Revises: 8b572e10ef77
Create Date: 2026-07-22 10:51:06.635451
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "3a2c5d36c963"
down_revision: str | None = "8b572e10ef77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Schema only. The curated license rows are reference data owned by `sync_licenses`
# (`make synclicenses`, run after migrate) — not seeded here. Existing `data_license` overrides are
# not carried across (they reset to inherit); the `platform_settings` singleton is reset so the
# default reverts to the env default until an admin sets one.


def upgrade() -> None:
    op.create_table(
        "data_licenses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("version", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column("short_description", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("reference_url", sqlmodel.sql.sqltypes.AutoString(length=1024), nullable=True),
        sa.Column("spdx_id", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column("created_by_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_data_licenses_created_by_id_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_licenses")),
    )
    op.create_index(op.f("ix_data_licenses_created_by_id"), "data_licenses", ["created_by_id"], unique=False)
    op.create_index(
        "ix_data_licenses_spdx_id",
        "data_licenses",
        ["spdx_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    for table in ("evaluation_groups", "evaluations"):
        op.add_column(table, sa.Column("data_license_id", sa.Uuid(), nullable=True))
        op.create_index(op.f(f"ix_{table}_data_license_id"), table, ["data_license_id"], unique=False)
        op.create_foreign_key(
            op.f(f"fk_{table}_data_license_id_data_licenses"), table, "data_licenses", ["data_license_id"], ["id"]
        )
        op.drop_column(table, "data_license")

    # Reset the singleton so the new NOT NULL FK column lands on an empty table (the default reverts
    # to the env default / the curated row `synclicenses` seeds; a materialized row returns on the
    # next admin write).
    op.execute("DELETE FROM platform_settings")
    op.add_column("platform_settings", sa.Column("default_license_id", sa.Uuid(), nullable=False))
    op.create_index(
        op.f("ix_platform_settings_default_license_id"), "platform_settings", ["default_license_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_platform_settings_default_license_id_data_licenses"),
        "platform_settings",
        "data_licenses",
        ["default_license_id"],
        ["id"],
    )
    op.drop_column("platform_settings", "default_license")


def downgrade() -> None:
    op.execute("DELETE FROM platform_settings")
    op.add_column("platform_settings", sa.Column("default_license", sa.VARCHAR(length=64), nullable=False))
    op.drop_constraint(
        op.f("fk_platform_settings_default_license_id_data_licenses"), "platform_settings", type_="foreignkey"
    )
    op.drop_index(op.f("ix_platform_settings_default_license_id"), table_name="platform_settings")
    op.drop_column("platform_settings", "default_license_id")

    for table in ("evaluations", "evaluation_groups"):
        op.add_column(table, sa.Column("data_license", sa.VARCHAR(length=64), nullable=True))
        op.drop_constraint(op.f(f"fk_{table}_data_license_id_data_licenses"), table, type_="foreignkey")
        op.drop_index(op.f(f"ix_{table}_data_license_id"), table_name=table)
        op.drop_column(table, "data_license_id")

    op.drop_index(
        "ix_data_licenses_spdx_id", table_name="data_licenses", postgresql_where=sa.text("deleted_at IS NULL")
    )
    op.drop_index(op.f("ix_data_licenses_created_by_id"), table_name="data_licenses")
    op.drop_table("data_licenses")
