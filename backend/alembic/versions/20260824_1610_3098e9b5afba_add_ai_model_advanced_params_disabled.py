"""add ai model advanced params disabled flag

Revision ID: 3098e9b5afba
Revises: bcdec73c99d9
Create Date: 2026-08-24 16:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3098e9b5afba"
down_revision: str | None = "bcdec73c99d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_models",
        sa.Column("advanced_params_disabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("ai_models", "advanced_params_disabled")
