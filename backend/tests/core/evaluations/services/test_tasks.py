"""Integration tests for `services.tasks` plus the scenario-level write/visibility helpers it relies on."""

from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import Scenario
from app.core.evaluations.schemas import TaskUpdateChanges
from app.core.evaluations.services.scenarios import authorize_scenario_mutation
from app.core.evaluations.services.scenarios import get_scenario_by_id
from app.core.evaluations.services.tasks import create_task
from app.core.evaluations.services.tasks import get_task
from app.core.evaluations.services.tasks import list_tasks
from app.core.evaluations.services.tasks import soft_delete_task
from app.core.evaluations.services.tasks import update_task
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.restore import restore_cutoff
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


# These suites exercise the live set; the window is inert but the services now
# require it explicitly, as the routes derive it.
_CUTOFF = restore_cutoff(get_settings())


async def _persist_scenario(
    db_session: AsyncSession, *, access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC
) -> tuple[Scenario, UUID]:
    """Persist group → evaluation → scenario; return the scenario and its owner id."""
    group = await persist_evaluation_group(db_session, access_level=access_level)
    evaluation = Evaluation(title="e", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    scenario = Scenario(evaluation_id=evaluation.id, name="S", description="d", position=0)
    db_session.add(scenario)
    await db_session.flush()
    return scenario, group.created_by_id


async def test_create_persists_under_scenario(db_session: AsyncSession) -> None:
    scenario, _ = await _persist_scenario(db_session)

    task = await create_task(db_session, scenario_id=scenario.id, name="T", description="d")

    assert task.scenario_id == scenario.id
    assert task.name == "T"
    assert task.deleted_at is None


async def test_get_task_cross_scenario_reads_as_missing(db_session: AsyncSession) -> None:
    scenario_a, _ = await _persist_scenario(db_session)
    scenario_b, _ = await _persist_scenario(db_session)
    task = await create_task(db_session, scenario_id=scenario_a.id, name="T", description="d")

    assert (await get_task(db_session, scenario_a.id, task.id)).id == task.id
    with pytest.raises(NotFoundError):
        await get_task(db_session, scenario_b.id, task.id)


async def test_update_writes_only_set_fields(db_session: AsyncSession) -> None:
    scenario, _ = await _persist_scenario(db_session)
    task = await create_task(db_session, scenario_id=scenario.id, name="before", description="keep")

    updated = await update_task(db_session, task, TaskUpdateChanges.model_validate({"name": "after"}))

    assert updated.name == "after"
    assert updated.description == "keep"


async def test_soft_delete_excludes_from_reads(db_session: AsyncSession) -> None:
    scenario, _ = await _persist_scenario(db_session)
    task = await create_task(db_session, scenario_id=scenario.id, name="T", description="d")

    await soft_delete_task(db_session, task, by_id=uuid4())

    with pytest.raises(NotFoundError):
        await get_task(db_session, scenario.id, task.id)


async def test_list_returns_scenario_tasks(db_session: AsyncSession) -> None:
    scenario, owner_id = await _persist_scenario(db_session)
    await create_task(db_session, scenario_id=scenario.id, name="A", description="d")
    await create_task(db_session, scenario_id=scenario.id, name="B", description="d")

    tasks, total = await list_tasks(
        db_session, scenario_id=scenario.id, caller_id=owner_id, limit=50, offset=0, deleted_cutoff=_CUTOFF
    )

    assert total == 2
    assert {task.name for task in tasks} == {"A", "B"}


async def test_get_task_private_group_scopes_to_owner_and_manager(db_session: AsyncSession) -> None:
    scenario, owner_id = await _persist_scenario(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    task = await create_task(db_session, scenario_id=scenario.id, name="T", description="d")

    with pytest.raises(NotFoundError):
        await get_task(db_session, scenario.id, task.id, caller_id=uuid4())
    assert (await get_task(db_session, scenario.id, task.id, caller_id=owner_id)).id == task.id
    assert (await get_task(db_session, scenario.id, task.id, caller_id=uuid4(), can_manage=True)).id == task.id


async def test_authorize_scenario_mutation_unknown_and_private(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await authorize_scenario_mutation(db_session, uuid4(), caller_id=uuid4(), can_manage=False)

    scenario, owner_id = await _persist_scenario(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    # A stranger can't even see the private group → 404 (no existence leak).
    with pytest.raises(NotFoundError):
        await authorize_scenario_mutation(db_session, scenario.id, caller_id=uuid4(), can_manage=False)
    # Owner and manager pass (no raise).
    await authorize_scenario_mutation(db_session, scenario.id, caller_id=owner_id, can_manage=False)
    await authorize_scenario_mutation(db_session, scenario.id, caller_id=uuid4(), can_manage=True)


async def test_authorize_scenario_mutation_public_not_owned_forbidden(db_session: AsyncSession) -> None:
    scenario, _ = await _persist_scenario(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)
    # Visible (public) but not owned and not manager → 403, not 404.
    with pytest.raises(ForbiddenError):
        await authorize_scenario_mutation(db_session, scenario.id, caller_id=uuid4(), can_manage=False)


async def test_get_scenario_by_id_scopes_visibility(db_session: AsyncSession) -> None:
    scenario, owner_id = await _persist_scenario(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    assert (await get_scenario_by_id(db_session, scenario.id, caller_id=owner_id, can_manage=False)).id == scenario.id
    with pytest.raises(NotFoundError):
        await get_scenario_by_id(db_session, scenario.id, caller_id=uuid4(), can_manage=False)
