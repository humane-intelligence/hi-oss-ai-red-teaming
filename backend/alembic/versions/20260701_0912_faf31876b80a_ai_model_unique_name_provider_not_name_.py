"""ai_model unique name+provider not name alone

Revision ID: faf31876b80a
Revises: af9006ae9924
Create Date: 2026-07-01 09:12:21.880025
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "faf31876b80a"
down_revision: str | None = "af9006ae9924"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_ai_models_name", table_name="ai_models", postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index(
        "ix_ai_models_name_provider",
        "ai_models",
        ["name", "provider"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    # The upgrade legalised the same name across providers, so the narrower
    # UNIQUE(name) can't be restored while any live duplicates exist. Check
    # before touching any index — fail-fast with the offending names so the
    # schema is left untouched (rather than relying on transactional-DDL
    # rollback to undo a drop) and the operator knows what to resolve first.
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
            f"Cannot restore UNIQUE(name): live models share a name across providers ({duplicates}). "
            "Resolve the duplicates before downgrading."
        )
        raise RuntimeError(msg)
    op.drop_index("ix_ai_models_name_provider", table_name="ai_models", postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index(
        "ix_ai_models_name", "ai_models", ["name"], unique=True, postgresql_where=sa.text("deleted_at IS NULL")
    )
