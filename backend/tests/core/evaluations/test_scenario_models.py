"""Model-level tests for `Scenario` — defaults, FK enforcement, cascade, relationship load."""

from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import Scenario
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


async def _persist_evaluation(db_session: AsyncSession) -> Evaluation:
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(
        title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    return evaluation


async def test_scenario_persists_fields(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)

    scenario = Scenario(evaluation_id=evaluation.id, name="S1", description="d", position=0)
    db_session.add(scenario)
    await db_session.flush()
    await db_session.refresh(scenario)

    assert scenario.id is not None
    assert scenario.evaluation_id == evaluation.id
    assert scenario.position == 0
    assert scenario.deleted_at is None


async def test_scenario_requires_existing_evaluation(db_session: AsyncSession) -> None:
    scenario = Scenario(evaluation_id=uuid4(), name="orphan", description="d", position=0)
    db_session.add(scenario)

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_deleting_evaluation_cascades_scenarios(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    db_session.add_all(
        [
            Scenario(evaluation_id=evaluation.id, name="S1", description="d", position=0),
            Scenario(evaluation_id=evaluation.id, name="S2", description="d", position=1),
        ]
    )
    await db_session.flush()

    await db_session.execute(delete(Evaluation).where(col(Evaluation.id) == evaluation.id))

    remaining = (
        (await db_session.execute(select(Scenario).where(col(Scenario.evaluation_id) == evaluation.id))).scalars().all()
    )
    assert remaining == []


async def test_evaluation_scenarios_relationship_loads(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    db_session.add_all(
        [
            Scenario(evaluation_id=evaluation.id, name="S1", description="d", position=0),
            Scenario(evaluation_id=evaluation.id, name="S2", description="d", position=1),
        ]
    )
    await db_session.flush()

    await db_session.refresh(evaluation, attribute_names=["scenarios"])

    assert {scenario.name for scenario in evaluation.scenarios} == {"S1", "S2"}
