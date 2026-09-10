"""add failure notification columns to outbound_emails

Revision ID: 44754f1b941d
Revises: 3a2c5d36c963
Create Date: 2026-07-28 20:45:52.816435
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "44754f1b941d"
down_revision: str | None = "3a2c5d36c963"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbound_emails", sa.Column("requested_by_user_id", sa.Uuid(), nullable=True))
    op.add_column("outbound_emails", sa.Column("batch_key", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_outbound_emails_batch_key"), "outbound_emails", ["batch_key"], unique=False)
    op.create_index(
        op.f("ix_outbound_emails_requested_by_user_id"),
        "outbound_emails",
        ["requested_by_user_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_outbound_emails_requested_by_user_id_users"),
        "outbound_emails",
        "users",
        ["requested_by_user_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_outbound_emails_requested_by_user_id_users"), "outbound_emails", type_="foreignkey")
    op.drop_index(op.f("ix_outbound_emails_requested_by_user_id"), table_name="outbound_emails")
    op.drop_index(op.f("ix_outbound_emails_batch_key"), table_name="outbound_emails")
    op.drop_column("outbound_emails", "batch_key")
    op.drop_column("outbound_emails", "requested_by_user_id")
