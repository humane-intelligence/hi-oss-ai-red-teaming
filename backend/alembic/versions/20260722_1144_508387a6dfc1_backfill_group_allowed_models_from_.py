"""backfill group allowed models from existing assignments

Revision ID: 508387a6dfc1
Revises: d71700be9df9
Create Date: 2026-07-22 11:44:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "508387a6dfc1"
down_revision: str | None = "d71700be9df9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Seed each group's subset with the DISTINCT live models its live evaluations
# already use, so existing groups keep working under the new subset rule. The
# ai_models join keeps a soft-deleted model out of the seed (a pre-cascade orphan
# assignment must not seed a dead model — the whole feature fails closed).
# Idempotent via NOT EXISTS; groups with no live usage stay empty (fail-closed).
_BACKFILL = sa.text("""
    INSERT INTO evaluation_group_ai_models (id, created_at, updated_at, evaluation_group_id, model_id)
    SELECT gen_random_uuid(), now(), now(), used.evaluation_group_id, used.model_id
    FROM (
        SELECT DISTINCT e.evaluation_group_id, eam.model_id
        FROM evaluation_ai_models AS eam
        JOIN evaluations AS e ON e.id = eam.evaluation_id
        JOIN ai_models AS am ON am.id = eam.model_id
        WHERE eam.deleted_at IS NULL AND e.deleted_at IS NULL AND am.deleted_at IS NULL
    ) AS used
    WHERE NOT EXISTS (
        SELECT 1 FROM evaluation_group_ai_models AS egam
        WHERE egam.evaluation_group_id = used.evaluation_group_id
          AND egam.model_id = used.model_id
          AND egam.deleted_at IS NULL
    )
""")


def upgrade() -> None:
    op.execute(_BACKFILL)


def downgrade() -> None:
    # No-op: backfilled rows are indistinguishable from operator-created ones, so
    # deleting them on downgrade would be wrong (the table drop lives upstream).
    pass
