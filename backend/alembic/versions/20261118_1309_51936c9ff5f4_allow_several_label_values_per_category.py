"""allow several label values per category

Revision ID: 51936c9ff5f4
Revises: 815a5242a6af
Create Date: 2026-11-18 13:09:21.406972
"""

from collections.abc import Sequence

from alembic import op

revision: str = "51936c9ff5f4"
down_revision: str | None = "815a5242a6af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Autogenerate does not see a primary-key change, so this is written by hand.
    op.drop_constraint(op.f("pk_conversation_group_labels"), "conversation_group_labels", type_="primary")
    op.create_primary_key(
        op.f("pk_conversation_group_labels"),
        "conversation_group_labels",
        ["conversation_group_id", "category_id", "value"],
    )


# One value per category again: keep the first of each, drop a bare selection (it had no equivalent).
_KEEP_ONE_VALUE_PER_CATEGORY = """
    DELETE FROM conversation_group_labels AS l
    WHERE l.value = ''
       OR EXISTS (
            SELECT 1 FROM conversation_group_labels AS o
            WHERE o.conversation_group_id = l.conversation_group_id
              AND o.category_id = l.category_id
              AND o.value <> ''
              AND o.value < l.value
       )
"""


def downgrade() -> None:
    op.execute(_KEEP_ONE_VALUE_PER_CATEGORY)
    op.drop_constraint(op.f("pk_conversation_group_labels"), "conversation_group_labels", type_="primary")
    op.create_primary_key(
        op.f("pk_conversation_group_labels"),
        "conversation_group_labels",
        ["conversation_group_id", "category_id"],
    )
