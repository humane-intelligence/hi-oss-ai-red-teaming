"""add invitation object scope

Revision ID: bdf068c5df59
Revises: 3d7518b07fa1
Create Date: 2026-06-10 16:09:56.422715
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "bdf068c5df59"
down_revision: str | None = "3d7518b07fa1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `objecttype` is already created by the object_role_assignments migration —
    # reuse it (create_type=False) so add_column doesn't try to CREATE TYPE again.
    objecttype = postgresql.ENUM("evaluation_group", name="objecttype", create_type=False)
    op.add_column("invitations", sa.Column("object_type", objecttype, nullable=True))
    op.add_column("invitations", sa.Column("object_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("invitations", "object_id")
    op.drop_column("invitations", "object_type")
