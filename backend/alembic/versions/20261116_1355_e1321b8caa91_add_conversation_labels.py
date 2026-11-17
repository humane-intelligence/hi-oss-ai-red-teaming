"""add conversation labels

Revision ID: e1321b8caa91
Revises: 1660a4c8ca69
Create Date: 2026-11-16 13:55:38.180570
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e1321b8caa91"
down_revision: str | None = "1660a4c8ca69"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_label_categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_id", sa.Uuid(), nullable=True),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column(
            "values", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["evaluations.id"],
            name=op.f("fk_evaluation_label_categories_evaluation_id_evaluations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_label_categories")),
    )
    op.create_index(
        "ix_evaluation_label_categories_evaluation_id_name",
        "evaluation_label_categories",
        ["evaluation_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "conversation_group_labels",
        sa.Column("conversation_group_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("value", sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["evaluation_label_categories.id"],
            name=op.f("fk_conversation_group_labels_category_id_evaluation_label_categories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_group_id"],
            ["conversation_groups.id"],
            name=op.f("fk_conversation_group_labels_conversation_group_id_conversation_groups"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("conversation_group_id", "category_id", name=op.f("pk_conversation_group_labels")),
    )
    op.create_index(
        "ix_conversation_group_labels_category_id", "conversation_group_labels", ["category_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_group_labels_category_id", table_name="conversation_group_labels")
    op.drop_table("conversation_group_labels")
    op.drop_index(
        "ix_evaluation_label_categories_evaluation_id_name",
        table_name="evaluation_label_categories",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("evaluation_label_categories")
