"""evaluation status enum

Revision ID: 509642f23c24
Revises: f84844841362
Create Date: 2026-06-09 10:52:45.747085
"""

from collections.abc import Sequence

from alembic import op

revision: str = "509642f23c24"
down_revision: str | None = "f84844841362"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# `evaluations.status` moves off the shared `publicationstatus` type (kept by
# `evaluation_groups`) onto its own `evaluationstatus`, whose values follow the
# evaluation state diagram. Autogenerate detects the type change but can't create
# the new PG enum or cast the column — hand-written. The table is empty, so a
# plain text cast suffices; no per-value data mapping is needed.


def upgrade() -> None:
    op.execute(
        "CREATE TYPE evaluationstatus AS ENUM "
        "('new', 'draft', 'under_review', 'rejected', 'approved', 'published', 'completed')"
    )
    op.execute("ALTER TABLE evaluations ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TABLE evaluations ALTER COLUMN status TYPE evaluationstatus USING status::text::evaluationstatus")
    op.execute("ALTER TABLE evaluations ALTER COLUMN status SET DEFAULT 'new'")


def downgrade() -> None:
    op.execute("ALTER TABLE evaluations ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE evaluations ALTER COLUMN status TYPE publicationstatus USING status::text::publicationstatus"
    )
    op.execute("ALTER TABLE evaluations ALTER COLUMN status SET DEFAULT 'draft'")
    op.execute("DROP TYPE evaluationstatus")
