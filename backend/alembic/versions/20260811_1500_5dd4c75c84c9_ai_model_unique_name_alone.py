"""ai_model unique name alone, not name+provider

Revision ID: 5dd4c75c84c9
Revises: 016524bf6c54
Create Date: 2026-08-11 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5dd4c75c84c9"
down_revision: str | None = "016524bf6c54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # An earlier migration (faf31876b80a) legalised the same name across
    # providers; this migration reverses that. Fail fast with the offending
    # names if any live rows would collide under the narrower constraint,
    # rather than letting the CREATE INDEX itself error out with a bare
    # constraint-violation message.
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT name FROM ai_models WHERE deleted_at IS NULL GROUP BY name HAVING count(*) > 1 ORDER BY name"
            )
        )
        .scalars()
        .all()
    )
    if duplicates:
        msg = (
            f"Cannot enforce UNIQUE(name): live models share a name across providers ({duplicates}). "
            "Resolve the duplicates before upgrading."
        )
        raise RuntimeError(msg)
    op.drop_index("ix_ai_models_name_provider", table_name="ai_models", postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index(
        "ix_ai_models_name", "ai_models", ["name"], unique=True, postgresql_where=sa.text("deleted_at IS NULL")
    )


def downgrade() -> None:
    op.drop_index("ix_ai_models_name", table_name="ai_models", postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index(
        "ix_ai_models_name_provider",
        "ai_models",
        ["name", "provider"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
