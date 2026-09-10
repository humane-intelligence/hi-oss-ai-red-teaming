"""add metrics access levels to evaluation groups

Revision ID: 33cfe103fa9c
Revises: e7edd05cecde
Create Date: 2026-07-08 12:03:30.539253
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "33cfe103fa9c"
down_revision: str | None = "e7edd05cecde"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Both columns share this one PG enum type, so it is created/dropped explicitly
# here (`create_type=False` below) rather than implicitly per column.
_METRICS_ACCESS_LEVEL = postgresql.ENUM(
    "inherit_group_access",
    "all_members",
    "members_personal_metrics",
    "owner_only",
    name="metricsaccesslevel",
    create_type=False,
)


def upgrade() -> None:
    _METRICS_ACCESS_LEVEL.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "evaluation_groups",
        sa.Column(
            "metrics_access_during",
            _METRICS_ACCESS_LEVEL,
            server_default=sa.text("'owner_only'"),
            nullable=False,
        ),
    )
    op.add_column(
        "evaluation_groups",
        sa.Column(
            "metrics_access_after",
            _METRICS_ACCESS_LEVEL,
            server_default=sa.text("'owner_only'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("evaluation_groups", "metrics_access_after")
    op.drop_column("evaluation_groups", "metrics_access_during")
    _METRICS_ACCESS_LEVEL.drop(op.get_bind(), checkfirst=False)
