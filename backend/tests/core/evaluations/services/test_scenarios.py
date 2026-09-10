"""Integration tests for `app.core.evaluations.services.scenarios` — service layer over a real DB."""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.filters import ScenarioFilters
from app.core.evaluations.models import Evaluation
from app.core.evaluations.schemas import ScenarioUpdateChanges
from app.core.evaluations.services.scenarios import create_scenario
from app.core.evaluations.services.scenarios import get_scenario
from app.core.evaluations.services.scenarios import list_scenarios
from app.core.evaluations.services.scenarios import reorder_scenarios
from app.core.evaluations.services.scenarios import soft_delete_scenario
from app.core.evaluations.services.scenarios import update_scenario
from app.core.evaluations.services.tasks import create_task
from app.core.evaluations.services.tasks import soft_delete_task
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.restore import restore_cutoff
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


# These suites exercise the live set; the window is inert but the services now
# require it explicitly, as the routes derive it.
_CUTOFF = restore_cutoff(get_settings())


async def _persist_evaluation(
    db_session: AsyncSession,
    *,
    title: str = "eval",
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
) -> Evaluation:
    group = await persist_evaluation_group(db_session, access_level=access_level)
    evaluation = Evaluation(
        title=title, description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    # `evaluation.created_by_id` mirrors the group owner, so visibility tests use
    # it as the "owner" caller without needing the group row back.
    return evaluation


async def test_create_appends_position(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)

    first = await create_scenario(db_session, evaluation_id=evaluation.id, name="A", description="d")
    second = await create_scenario(db_session, evaluation_id=evaluation.id, name="B", description="d")

    assert first.position == 0
    assert second.position == 1


async def test_create_unknown_evaluation_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await create_scenario(db_session, evaluation_id=uuid4(), name="A", description="d")


async def test_get_scenario_cross_evaluation_reads_as_missing(db_session: AsyncSession) -> None:
    eval_a = await _persist_evaluation(db_session, title="A")
    eval_b = await _persist_evaluation(db_session, title="B")
    scenario = await create_scenario(db_session, evaluation_id=eval_a.id, name="S", description="d")

    # Correct parent resolves; a foreign parent 404s even though the id exists.
    assert (await get_scenario(db_session, eval_a.id, scenario.id)).id == scenario.id
    with pytest.raises(NotFoundError):
        await get_scenario(db_session, eval_b.id, scenario.id)


async def test_get_scenario_with_tasks_loads_only_live_tasks(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    scenario = await create_scenario(db_session, evaluation_id=evaluation.id, name="S", description="d")
    first = await create_task(db_session, scenario_id=scenario.id, name="T1", description="d")
    second = await create_task(db_session, scenario_id=scenario.id, name="T2", description="d")
    doomed = await create_task(db_session, scenario_id=scenario.id, name="doomed", description="d")
    await soft_delete_task(db_session, doomed, by_id=uuid4())

    fetched = await get_scenario(db_session, evaluation.id, scenario.id, with_tasks=True)

    # The soft-deleted task is filtered out of the eager-loaded collection.
    assert {task.id for task in fetched.tasks} == {first.id, second.id}


async def test_get_scenario_without_tasks_flag_omits_eager_load(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    scenario = await create_scenario(db_session, evaluation_id=evaluation.id, name="S", description="d")
    await create_task(db_session, scenario_id=scenario.id, name="T", description="d")

    # Default path takes no `with_tasks`; the row still resolves for mutation/existence checks.
    assert (await get_scenario(db_session, evaluation.id, scenario.id)).id == scenario.id


async def test_update_writes_only_set_fields(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    scenario = await create_scenario(db_session, evaluation_id=evaluation.id, name="before", description="keep")

    changes = ScenarioUpdateChanges.model_validate({"name": "after"})
    updated = await update_scenario(db_session, scenario, changes)

    assert updated.name == "after"
    assert updated.description == "keep"


async def test_soft_delete_excludes_from_reads(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    scenario = await create_scenario(db_session, evaluation_id=evaluation.id, name="S", description="d")

    await soft_delete_scenario(db_session, scenario, by_id=uuid4())

    with pytest.raises(NotFoundError):
        await get_scenario(db_session, evaluation.id, scenario.id)


async def test_reorder_rewrites_positions(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    first = await create_scenario(db_session, evaluation_id=evaluation.id, name="A", description="d")
    second = await create_scenario(db_session, evaluation_id=evaluation.id, name="B", description="d")

    reordered = await reorder_scenarios(db_session, evaluation.id, [second.id, first.id])

    assert [s.id for s in reordered] == [second.id, first.id]
    assert second.position == 0
    assert first.position == 1


async def test_reorder_rejects_unknown_id(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    scenario = await create_scenario(db_session, evaluation_id=evaluation.id, name="A", description="d")

    with pytest.raises(NotFoundError):
        await reorder_scenarios(db_session, evaluation.id, [scenario.id, uuid4()])


async def test_reorder_rejects_incomplete_set(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    first = await create_scenario(db_session, evaluation_id=evaluation.id, name="A", description="d")
    await create_scenario(db_session, evaluation_id=evaluation.id, name="B", description="d")

    with pytest.raises(ConflictError):
        await reorder_scenarios(db_session, evaluation.id, [first.id])


async def test_list_filters_by_evaluation_and_search(db_session: AsyncSession) -> None:
    eval_a = await _persist_evaluation(db_session, title="A")
    eval_b = await _persist_evaluation(db_session, title="B")
    await create_scenario(db_session, evaluation_id=eval_a.id, name="needle", description="d")
    await create_scenario(db_session, evaluation_id=eval_a.id, name="haystack", description="d")
    await create_scenario(db_session, evaluation_id=eval_b.id, name="needle", description="d")

    by_eval, total_eval = await list_scenarios(
        db_session,
        caller_id=uuid4(),
        filters=ScenarioFilters(evaluation_id=eval_a.id),
        order_by="position",
        limit=50,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )
    by_search, total_search = await list_scenarios(
        db_session,
        caller_id=uuid4(),
        filters=ScenarioFilters(search="needle"),
        order_by="position",
        limit=50,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert total_eval == 2
    assert all(s.evaluation_id == eval_a.id for s in by_eval)
    assert total_search == 2  # one per evaluation
    assert all(s.name == "needle" for s in by_search)


async def test_get_scenario_private_group_not_owned_reads_as_missing(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    owner_id = evaluation.created_by_id
    scenario = await create_scenario(db_session, evaluation_id=evaluation.id, name="secret", description="d")

    # A stranger cannot resolve a scenario under a private group they don't own.
    with pytest.raises(NotFoundError):
        await get_scenario(db_session, evaluation.id, scenario.id, caller_id=uuid4())
    # The owner — and a manager — resolve it.
    assert (await get_scenario(db_session, evaluation.id, scenario.id, caller_id=owner_id)).id == scenario.id
    assert (
        await get_scenario(db_session, evaluation.id, scenario.id, caller_id=uuid4(), can_manage=True)
    ).id == scenario.id


async def test_list_scopes_private_group_to_owner_and_manager(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    owner_id = evaluation.created_by_id
    await create_scenario(db_session, evaluation_id=evaluation.id, name="secret", description="d")

    _, stranger_total = await list_scenarios(
        db_session,
        caller_id=uuid4(),
        filters=ScenarioFilters(),
        order_by="position",
        limit=50,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )
    _, owner_total = await list_scenarios(
        db_session,
        caller_id=owner_id,
        filters=ScenarioFilters(),
        order_by="position",
        limit=50,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )
    _, manager_total = await list_scenarios(
        db_session,
        caller_id=uuid4(),
        can_manage=True,
        filters=ScenarioFilters(),
        order_by="position",
        limit=50,
        offset=0,
        deleted_cutoff=_CUTOFF,
    )

    assert stranger_total == 0
    assert owner_total == 1
    assert manager_total == 1
