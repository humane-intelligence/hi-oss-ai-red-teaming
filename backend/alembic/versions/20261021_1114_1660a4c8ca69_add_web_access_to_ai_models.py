"""add web_access to ai_models

Revision ID: 1660a4c8ca69
Revises: b11ddc8b3dce
Create Date: 2026-10-21 11:14:32.994498
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1660a4c8ca69"
down_revision: str | None = "b11ddc8b3dce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_models", sa.Column("web_access", sa.Boolean(), server_default=sa.text("false"), nullable=False))


def downgrade() -> None:
    op.drop_column("ai_models", "web_access")
