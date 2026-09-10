"""add warmup_enabled to ai_models

Revision ID: d765a6a3b044
Revises: 4c95955607dc
Create Date: 2026-07-03 09:35:19.380241
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d765a6a3b044"
down_revision: str | None = "4c95955607dc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_models",
        sa.Column("warmup_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("ai_models", "warmup_enabled")
