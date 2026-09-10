"""baseline

Revision ID: d1a2b3c4
Revises:
Create Date: 2026-05-13 00:00:00.000000
"""

from collections.abc import Sequence

revision: str = "d1a2b3c4"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
