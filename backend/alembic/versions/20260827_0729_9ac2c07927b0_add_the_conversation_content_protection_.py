"""add the conversation-content protection flag and its per-row columns

Revision ID: 9ac2c07927b0
Revises: d3f7b1c2a904
Create Date: 2026-08-27 07:29:32.118133
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9ac2c07927b0"
down_revision: str | None = "d3f7b1c2a904"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations", sa.Column("content_protected", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.add_column(
        "data_licenses",
        sa.Column("protects_conversation_data", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "messages", sa.Column("content_encrypted", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )


def downgrade() -> None:
    # Dropping the discriminator while `messages.content` still holds ciphertext is unrecoverable in
    # place: a later upgrade lands those rows at the `false` server default, and every read then serves
    # `<kid>:v2:…` verbatim as message text. Refuse instead of corrupting. Skipped under `--sql`, where
    # there is no connection to ask.
    context = op.get_context()
    if not context.as_sql:
        sealed = op.get_bind().execute(sa.text("SELECT 1 FROM messages WHERE content_encrypted LIMIT 1")).first()
        if sealed is not None:
            msg = (
                "messages.content_encrypted is set on at least one row: dropping it would leave sealed "
                "text unreadable and served as plaintext. Decrypt or delete those rows first."
            )
            raise RuntimeError(msg)
    op.drop_column("messages", "content_encrypted")
    op.drop_column("data_licenses", "protects_conversation_data")
    op.drop_column("conversations", "content_protected")
