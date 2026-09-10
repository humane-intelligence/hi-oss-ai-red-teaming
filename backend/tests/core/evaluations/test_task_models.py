"""Model-level tests for `Task` — FK enforcement and cascade (direct + transitive)."""

from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


async def _persist_scenario(db_session: AsyncSession) -> Scenario:
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(
        title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    scenario = Scenario(evaluation_id=evaluation.id, name="S", description="d", position=0)
    db_session.add(scenario)
    await db_session.flush()
    return scenario


async def test_task_persists_fields(db_session: AsyncSession) -> None:
    scenario = await _persist_scenario(db_session)

    task = Task(scenario_id=scenario.id, name="T1", description="d")
    db_session.add(task)
    await db_session.flush()
    await db_session.refresh(task)

    assert task.id is not None
    assert task.scenario_id == scenario.id
    assert task.deleted_at is None


async def test_task_requires_existing_scenario(db_session: AsyncSession) -> None:
    task = Task(scenario_id=uuid4(), name="orphan", description="d")
    db_session.add(task)

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_deleting_scenario_cascades_tasks(db_session: AsyncSession) -> None:
    scenario = await _persist_scenario(db_session)
    db_session.add_all(
        [
            Task(scenario_id=scenario.id, name="T1", description="d"),
            Task(scenario_id=scenario.id, name="T2", description="d"),
        ]
    )
    await db_session.flush()

    await db_session.execute(delete(Scenario).where(col(Scenario.id) == scenario.id))

    remaining = (await db_session.execute(select(Task).where(col(Task.scenario_id) == scenario.id))).scalars().all()
    assert remaining == []


async def test_deleting_evaluation_cascades_to_tasks(db_session: AsyncSession) -> None:
    # Transitive cascade: evaluation → scenarios → tasks, all via DB ON DELETE CASCADE.
    scenario = await _persist_scenario(db_session)
    db_session.add(Task(scenario_id=scenario.id, name="T1", description="d"))
    await db_session.flush()

    await db_session.execute(delete(Evaluation).where(col(Evaluation.id) == scenario.evaluation_id))

    remaining = (await db_session.execute(select(Task).where(col(Task.scenario_id) == scenario.id))).scalars().all()
    assert remaining == []
