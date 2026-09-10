"""add annotations

Schema only, no backfill. `annotation_labels` is already created in its two-kind shape by
`dbb137aac716`, so nothing here alters it — this migration only adds the annotations table.

Revision ID: 308cc687e191
Revises: dbb137aac716
Create Date: 2026-08-28 13:31:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "308cc687e191"
down_revision: str | None = "dbb137aac716"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("label_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_group_id", sa.Uuid(), nullable=False),
        # No delete rule on `message_id` / `label_id` / `created_by_id`: an annotated
        # message or a referenced label cannot be hard-deleted out from under the row,
        # and authorship survives the author's tombstone.
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], name=op.f("fk_annotations_message_id_messages")),
        sa.ForeignKeyConstraint(
            ["label_id"], ["annotation_labels.id"], name=op.f("fk_annotations_label_id_annotation_labels")
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], name=op.f("fk_annotations_created_by_id_users")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_annotations_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["evaluations.id"],
            name=op.f("fk_annotations_evaluation_id_evaluations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_group_id"],
            ["evaluation_groups.id"],
            name=op.f("fk_annotations_evaluation_group_id_evaluation_groups"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_annotations")),
    )
    op.create_index(op.f("ix_annotations_message_id"), "annotations", ["message_id"])
    op.create_index(op.f("ix_annotations_label_id"), "annotations", ["label_id"])
    op.create_index(op.f("ix_annotations_created_by_id"), "annotations", ["created_by_id"])
    op.create_index("ix_annotations_conversation_id", "annotations", ["conversation_id"])
    op.create_index("ix_annotations_evaluation_id", "annotations", ["evaluation_id"])
    op.create_index("ix_annotations_evaluation_group_id", "annotations", ["evaluation_group_id"])
    op.create_index(
        "ix_annotations_message_label_author",
        "annotations",
        ["message_id", "label_id", "created_by_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("annotations")
