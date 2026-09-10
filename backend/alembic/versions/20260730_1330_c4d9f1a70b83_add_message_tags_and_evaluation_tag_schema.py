"""add message tags and the evaluation tag schema

Revision ID: c4d9f1a70b83
Revises: 939e05f02fa0
Create Date: 2026-07-30 13:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c4d9f1a70b83"
down_revision: str | None = "939e05f02fa0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "tags", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )
    # `tags_enabled` defaults true so evaluations that predate the flag keep tagging; `tags_restricted`
    # defaults false, i.e. free-form keys once tagging is on. `conversations.tags` already shipped in
    # 939e05f02fa0, so this revision adds only what the admin tag schema and per-message tags need.
    op.add_column(
        "evaluations", sa.Column("tags_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False)
    )
    op.add_column(
        "evaluations", sa.Column("tags_restricted", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.create_table(
        "evaluation_tag_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("key", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["evaluations.id"],
            name=op.f("fk_evaluation_tag_keys_evaluation_id_evaluations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_tag_keys")),
    )
    op.create_index(
        "ix_evaluation_tag_keys_evaluation_id_key",
        "evaluation_tag_keys",
        ["evaluation_id", "key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    # Lossy: the column holds authored per-message tags, the table holds the admin tag schema, and the
    # two flags hold an operator's tagging policy. A re-upgrade restores the schema with defaults, not
    # the data — so a round-trip silently re-enables free-form tagging.
    op.drop_index(
        "ix_evaluation_tag_keys_evaluation_id_key",
        table_name="evaluation_tag_keys",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("evaluation_tag_keys")
    op.drop_column("evaluations", "tags_restricted")
    op.drop_column("evaluations", "tags_enabled")
    op.drop_column("messages", "tags")
