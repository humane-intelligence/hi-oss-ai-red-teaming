"""require scenario_id on conversations and conversation groups

Revision ID: 4fd9bfdbdc1f
Revises: 9ac2c07927b0
Create Date: 2026-08-28 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4fd9bfdbdc1f"
down_revision: str | None = "9ac2c07927b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Backfill before NOT NULL, coherence first: a group inherits its earliest member's
# scenario (never contradicting a member that already chose one), then falls back to
# the evaluation's first live scenario; an evaluation left with neither gets a
# synthetic 'General' scenario first, so no data is dropped. Conversations then
# inherit their group's scenario.
#
# None of the three overwrites a non-NULL `scenario_id`. A conversation that already
# disagreed with its group keeps its own scenario: that pair was reachable before this
# revision (create validated the scenario against the *evaluation*, never against the
# group), and no non-arbitrary repair exists — repointing the conversation would
# discard which scenario its task completions are keyed to. Coherence is therefore
# forward-only, enforced by the create/move 409s, not an invariant established here
# over pre-existing rows.
#
# Module-level so the migration behavior tests can exercise the SQL against
# seeded data (the upgrade itself runs over empty tables at test-session setup).
_CREATE_MISSING_SCENARIOS = sa.text(
    """
    INSERT INTO scenarios (id, name, description, evaluation_id, position)
    SELECT gen_random_uuid(), 'General',
           'Conversations recorded before this evaluation had scenarios.',
           e.id, 0
    FROM evaluations e
    WHERE NOT EXISTS (
        SELECT 1 FROM scenarios s WHERE s.evaluation_id = e.id AND s.deleted_at IS NULL
    )
    AND EXISTS (
        SELECT 1 FROM conversation_groups g
        WHERE g.evaluation_id = e.id AND g.scenario_id IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM conversations c
            WHERE c.conversation_group_id = g.id AND c.scenario_id IS NOT NULL
        )
    )
    """
)

_ASSIGN_GROUP_SCENARIOS = sa.text(
    """
    UPDATE conversation_groups g SET scenario_id = COALESCE(
        (SELECT c.scenario_id FROM conversations c
         WHERE c.conversation_group_id = g.id AND c.scenario_id IS NOT NULL
         ORDER BY c.created_at, c.id
         LIMIT 1),
        (SELECT s.id FROM scenarios s
         WHERE s.evaluation_id = g.evaluation_id AND s.deleted_at IS NULL
         ORDER BY s.position, s.id
         LIMIT 1)
    )
    WHERE g.scenario_id IS NULL
    """
)

_ASSIGN_CONVERSATION_SCENARIOS = sa.text(
    """
    UPDATE conversations t SET scenario_id = (
        SELECT g.scenario_id FROM conversation_groups g WHERE g.id = t.conversation_group_id
    )
    WHERE t.scenario_id IS NULL
    """
)


def upgrade() -> None:
    op.execute(_CREATE_MISSING_SCENARIOS)
    op.execute(_ASSIGN_GROUP_SCENARIOS)
    op.execute(_ASSIGN_CONVERSATION_SCENARIOS)
    op.alter_column("conversation_groups", "scenario_id", existing_type=sa.UUID(), nullable=False)
    op.drop_constraint(op.f("fk_conversation_groups_scenario_id_scenarios"), "conversation_groups", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_conversation_groups_scenario_id_scenarios"),
        "conversation_groups",
        "scenarios",
        ["scenario_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("conversations", "scenario_id", existing_type=sa.UUID(), nullable=False)
    op.drop_constraint(op.f("fk_conversations_scenario_id_scenarios"), "conversations", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_conversations_scenario_id_scenarios"),
        "conversations",
        "scenarios",
        ["scenario_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # Synthetic 'General' scenarios are kept: real conversations and flags may
    # reference them by now. Only the NOT NULL and the FK action are reverted.
    op.drop_constraint(op.f("fk_conversations_scenario_id_scenarios"), "conversations", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_conversations_scenario_id_scenarios"),
        "conversations",
        "scenarios",
        ["scenario_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("conversations", "scenario_id", existing_type=sa.UUID(), nullable=True)
    op.drop_constraint(op.f("fk_conversation_groups_scenario_id_scenarios"), "conversation_groups", type_="foreignkey")
    op.create_foreign_key(
        op.f("fk_conversation_groups_scenario_id_scenarios"),
        "conversation_groups",
        "scenarios",
        ["scenario_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("conversation_groups", "scenario_id", existing_type=sa.UUID(), nullable=True)
