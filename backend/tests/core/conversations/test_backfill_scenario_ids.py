"""Behavior tests for the scenario_id backfill data migration (revision `4fd9bfdbdc1f`).

The upgrade only runs once at test-session setup (over empty tables), so its
substantive SQL is exercised here directly against seeded data. Post-migration the
columns are NOT NULL, making the backfilled state unrepresentable — each test
drops the constraint first; the fixture's outer-transaction rollback restores it.
"""

import uuid
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import Scenario
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration

_REVISION = "4fd9bfdbdc1f"


def _migration_module() -> Any:
    """Load the migration module via Alembic (no fragile file-path import)."""
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    return script.get_revision(_REVISION).module


async def _run_backfill(db_session: AsyncSession) -> None:
    module = _migration_module()
    await db_session.execute(module._CREATE_MISSING_SCENARIOS)
    await db_session.execute(module._ASSIGN_GROUP_SCENARIOS)
    await db_session.execute(module._ASSIGN_CONVERSATION_SCENARIOS)


async def _allow_null_scenario_id(db_session: AsyncSession) -> None:
    for table in ("conversations", "conversation_groups"):
        await db_session.execute(text(f"ALTER TABLE {table} ALTER COLUMN scenario_id DROP NOT NULL"))


async def _persist_chain(
    db_session: AsyncSession,
) -> tuple[Evaluation, ConversationGroup, Conversation, Scenario]:
    """A live evaluation with one scenario, one group and one conversation pointing at it."""
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(
        title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    scenario = Scenario(name="s0", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()
    alias = f"m-{uuid.uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    db_session.add(assignment)
    await db_session.flush()
    conversation_group = ConversationGroup(
        user_id=group.created_by_id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id
    )
    db_session.add(conversation_group)
    await db_session.flush()
    conversation = Conversation(
        user_id=group.created_by_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=scenario.id,
    )
    db_session.add(conversation)
    await db_session.flush()
    return evaluation, conversation_group, conversation, scenario


async def _null_out_scenario_ids(db_session: AsyncSession, evaluation_id: uuid.UUID) -> None:
    for table in ("conversations", "conversation_groups"):
        await db_session.execute(
            text(f"UPDATE {table} SET scenario_id = NULL WHERE evaluation_id = :evaluation_id"),  # noqa: S608 — literal table names
            {"evaluation_id": evaluation_id},
        )


async def _live_scenarios(db_session: AsyncSession, evaluation_id: uuid.UUID) -> list[Scenario]:
    return list(
        (await db_session.execute(Scenario.live_select().where(col(Scenario.evaluation_id) == evaluation_id)))
        .scalars()
        .all()
    )


async def test_backfill_assigns_first_live_scenario(db_session: AsyncSession) -> None:
    await _allow_null_scenario_id(db_session)
    evaluation, conversation_group, conversation, scenario = await _persist_chain(db_session)
    expected_scenario_id = scenario.id
    tombstoned = Scenario(name="dead", description="d", evaluation_id=evaluation.id, position=0)
    tombstoned.soft_delete(None)
    later = Scenario(name="s1", description="d", evaluation_id=evaluation.id, position=1)
    db_session.add_all([tombstoned, later])
    await db_session.flush()
    await _null_out_scenario_ids(db_session, evaluation.id)

    await _run_backfill(db_session)

    await db_session.refresh(conversation)
    await db_session.refresh(conversation_group)
    assert conversation.scenario_id == expected_scenario_id
    assert conversation_group.scenario_id == expected_scenario_id
    assert len(await _live_scenarios(db_session, evaluation.id)) == 2  # no synthetic row


async def test_backfill_creates_synthetic_scenario_when_none_live(db_session: AsyncSession) -> None:
    await _allow_null_scenario_id(db_session)
    evaluation, conversation_group, conversation, scenario = await _persist_chain(db_session)
    scenario.soft_delete(None)
    await db_session.flush()
    await _null_out_scenario_ids(db_session, evaluation.id)

    await _run_backfill(db_session)

    live = await _live_scenarios(db_session, evaluation.id)
    assert [(s.name, s.position) for s in live] == [("General", 0)]
    await db_session.refresh(conversation)
    await db_session.refresh(conversation_group)
    assert conversation.scenario_id == live[0].id
    assert conversation_group.scenario_id == live[0].id


async def test_backfill_skips_evaluations_without_orphans(db_session: AsyncSession) -> None:
    await _allow_null_scenario_id(db_session)
    evaluation, _, conversation, scenario = await _persist_chain(db_session)
    expected_scenario_id = scenario.id

    await _run_backfill(db_session)

    await db_session.refresh(conversation)
    assert conversation.scenario_id == expected_scenario_id
    assert len(await _live_scenarios(db_session, evaluation.id)) == 1  # no synthetic row


async def test_backfill_member_inherits_its_groups_scenario(db_session: AsyncSession) -> None:
    # A group that already chose a scenario wins over the evaluation's first live one:
    # the member inherits the group's, so the backfill can't split a group from its members.
    await _allow_null_scenario_id(db_session)
    evaluation, conversation_group, conversation, first_by_position = await _persist_chain(db_session)
    chosen = Scenario(name="chosen", description="d", evaluation_id=evaluation.id, position=1)
    db_session.add(chosen)
    await db_session.flush()
    chosen_id = chosen.id
    conversation_group.scenario_id = chosen_id
    db_session.add(conversation_group)
    await db_session.flush()
    await db_session.execute(
        text("UPDATE conversations SET scenario_id = NULL WHERE evaluation_id = :evaluation_id"),
        {"evaluation_id": evaluation.id},
    )

    await _run_backfill(db_session)

    await db_session.refresh(conversation)
    assert conversation.scenario_id == chosen_id
    assert chosen_id != first_by_position.id


async def test_backfill_group_inherits_its_members_scenario(db_session: AsyncSession) -> None:
    # The inverse legacy shape: a scenario-less group whose member already chose one —
    # the member's scenario wins over the evaluation's first live one.
    await _allow_null_scenario_id(db_session)
    evaluation, conversation_group, conversation, first_by_position = await _persist_chain(db_session)
    chosen = Scenario(name="chosen", description="d", evaluation_id=evaluation.id, position=1)
    db_session.add(chosen)
    await db_session.flush()
    chosen_id = chosen.id
    conversation.scenario_id = chosen_id
    db_session.add(conversation)
    await db_session.flush()
    await db_session.execute(
        text("UPDATE conversation_groups SET scenario_id = NULL WHERE evaluation_id = :evaluation_id"),
        {"evaluation_id": evaluation.id},
    )

    await _run_backfill(db_session)

    await db_session.refresh(conversation_group)
    assert conversation_group.scenario_id == chosen_id
    assert chosen_id != first_by_position.id
    assert len(await _live_scenarios(db_session, evaluation.id)) == 2  # no synthetic row
