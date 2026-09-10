"""add ai model inactivity tracking columns

Revision ID: a495e784a11a
Revises: f1d5ed29d7ff
Create Date: 2026-08-19 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a495e784a11a"
down_revision: str | None = "f1d5ed29d7ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_models", sa.Column("inactivity_alert_hours", sa.Integer(), nullable=True))
    op.add_column("ai_models", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ai_models", sa.Column("last_warmup_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ai_models", sa.Column("inactivity_alerted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_models", "inactivity_alerted_at")
    op.drop_column("ai_models", "last_warmup_at")
    op.drop_column("ai_models", "last_used_at")
    op.drop_column("ai_models", "inactivity_alert_hours")
