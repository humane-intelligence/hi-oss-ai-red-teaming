"""add capability_mismatch to ai_models

Revision ID: bcdec73c99d9
Revises: a495e784a11a
Create Date: 2026-08-20 11:06:54.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "bcdec73c99d9"
down_revision: str | None = "a495e784a11a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_models", sa.Column("capability_mismatch", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("ai_models", "capability_mismatch")
