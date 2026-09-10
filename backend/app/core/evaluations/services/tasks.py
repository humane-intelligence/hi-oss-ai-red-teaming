"""Task service — pure async functions over an `AsyncSession`.

Mirrors `services.scenarios` one level deeper: routers stay thin and raise
`APIError` subclasses; soft-deleted rows are filtered per-statement via the
`live_*` factories on `BaseModel`. Authorization is inherited from the parent
scenario's evaluation group:

* **Reads** (`get_task`, `list_tasks`) take a ``caller_id`` and resolve only
  tasks whose scenario, evaluation, and parent group are live and the group is
  `public`-or-member (`group_visible_to`). ``can_manage``
  (`evaluation_groups:manage`) lifts the public-or-member predicate, never the
  liveness requirement.
* **Writes** (`create_task`/`update_task`/`soft_delete_task`) are gated at the
  route by `authorize_scenario_mutation` (an in-group role granting
  `evaluations:update`, or the `evaluation_groups:manage` break-glass), so the
  service functions here omit ``caller_id`` — write access has already been
  asserted.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.evaluations.access import join_visible_evaluation_group
from app.core.evaluations.models import TASK_DEFAULT_ORDER
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.evaluations.schemas import TaskUpdateChanges
from app.core.exceptions import NotFoundError
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row


def _scope_to_visible(statement: Select[tuple[Task]], *, caller_id: UUID, can_manage: bool) -> Select[tuple[Task]]:
    """Constrain a task `Select` to tasks whose scenario chain is live and caller-visible.

    Mirrors `scenarios._scope_to_visible` one level deeper: a task resolves only
    when its scenario, evaluation, and parent group are all live and the group is
    `public`-or-member. `can_manage` lifts the public-or-member predicate, never the
    liveness requirement.
    """
    statement = statement.join(Scenario, col(Task.scenario_id) == col(Scenario.id)).where(
        col(Scenario.deleted_at).is_(None)
    )
    return join_visible_evaluation_group(
        statement, col(Scenario.evaluation_id), caller_id=caller_id, can_manage=can_manage
    )


async def get_task(
    session: AsyncSession,
    scenario_id: UUID,
    task_id: UUID,
    *,
    caller_id: UUID | None = None,
    can_manage: bool = False,
    for_update: bool = False,
) -> Task:
    """Fetch the live task ``task_id`` belonging to ``scenario_id`` (cross-scenario id → clean 404).

    On the **read** path, pass ``caller_id`` to inherit the parent scenario's
    visibility (scenario + evaluation + group live, group `public`-or-member);
    ``can_manage`` lifts the public-or-member predicate but not liveness.

    Mutation paths omit ``caller_id`` and pass ``for_update=True``. The coupling
    to watch: this lookup's ``live_select`` checks only the **task** row's
    liveness, not the scenario/evaluation/group chain — it relies on a prior
    `authorize_scenario_mutation` (same transaction) for parent liveness + write
    access. Without it, a task under a soft-deleted scenario still resolves.

    Raises:
        NotFoundError: If no live task ``task_id`` exists in the scenario (or the
            parent isn't visible to ``caller_id`` when supplied).
    """
    statement = Task.live_select().where(
        col(Task.scenario_id) == scenario_id,
        col(Task.id) == task_id,
    )
    if caller_id is not None:
        statement = _scope_to_visible(statement, caller_id=caller_id, can_manage=can_manage)
    if for_update:
        statement = statement.with_for_update()
    task = (await session.execute(statement)).scalar_one_or_none()
    if task is None:
        raise NotFoundError(f"Task {task_id} not found in scenario {scenario_id}.")
    return task


async def list_tasks(
    session: AsyncSession,
    *,
    scenario_id: UUID,
    caller_id: UUID,
    can_manage: bool = False,
    deleted: bool = False,
    limit: int,
    offset: int,
    deleted_cutoff: datetime,
) -> tuple[list[Task], int]:
    """Return one page of live tasks of ``scenario_id`` visible to ``caller_id``.

    The visibility scope (scenario → evaluation → group, `public`-or-member) is
    applied in `_scope_to_visible` before paging. Ordered by `TASK_DEFAULT_ORDER`
    — the same ordering the `Scenario.tasks` embed uses, so the list and the
    embedded view never diverge.

    ``deleted`` swaps the live set for the tombstones inside the restore window,
    most-recently-deleted first (this list takes no client `order_by`), scoped to the
    caller's own deletes unless ``can_manage``.
    ``deleted_cutoff`` comes from the route, like `get_restorable_task`'s — reaching for
    the global settings here would let the listing and the restore disagree on the window.
    """
    if deleted:
        statement = _scope_to_visible(
            deleted_select(Task, deleted_cutoff, deleted_by=None if can_manage else caller_id),
            caller_id=caller_id,
            can_manage=can_manage,
        ).where(col(Task.scenario_id) == scenario_id)
        statement = statement.order_by(col(Task.deleted_at).desc(), col(Task.id))
    else:
        statement = (
            _scope_to_visible(Task.live_select(), caller_id=caller_id, can_manage=can_manage)
            .where(col(Task.scenario_id) == scenario_id)
            .order_by(*TASK_DEFAULT_ORDER)
        )
    return await paginate(session, statement, limit=limit, offset=offset)


async def create_task(session: AsyncSession, *, scenario_id: UUID, name: str, description: str) -> Task:
    """Create a task under ``scenario_id``.

    A plain insert: tasks are unordered, so there is no position to assign and no
    parent lock to take. The route's `authorize_scenario_mutation` has already
    resolved the live parent scenario and the caller's write access, so the FK is
    guaranteed to resolve.
    """
    task = Task(scenario_id=scenario_id, name=name, description=description)
    session.add(task)
    await session.flush()
    await session.refresh(task, attribute_names=["created_at", "updated_at"])
    return task


async def update_task(session: AsyncSession, task: Task, changes: TaskUpdateChanges) -> Task:
    """Apply ``changes`` to ``task`` — writes only fields in `changes.model_fields_set`.

    Content-only (`name` / `description`); `scenario_id` is immutable here.
    """
    for field in changes.model_fields_set:
        setattr(task, field, getattr(changes, field))
    session.add(task)
    await session.flush()
    await session.refresh(task, attribute_names=["updated_at"])
    return task


async def get_restorable_task(
    session: AsyncSession,
    scenario_id: UUID,
    task_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    deleted_cutoff: datetime,
) -> Task:
    """Fetch the tombstoned task of ``scenario_id`` this caller may restore.

    An id from another scenario, another actor's delete, one outside the window, or one
    whose parent scenario (or its evaluation/group) is soft-deleted all read as missing
    — a task under a dead scenario is unreachable, and reviving the scenario is what
    brings it back. The route authorizes via `authorize_scenario_mutation` first, which
    is also what resolves the parent chain for a *live* scenario, so this adds only the
    liveness predicates that the tombstoned-task lookup cannot inherit, plus
    `list_tasks`' deleter predicate so the restore reaches exactly what the deleted
    listing offered.

    Raises:
        NotFoundError: If no such restorable task exists.
    """
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = (
        deleted_select(Task, deleted_cutoff, deleted_by=None if can_manage else caller_id)
        .where(col(Task.scenario_id) == scenario_id, col(Task.id) == task_id)
        .join(Scenario, col(Task.scenario_id) == col(Scenario.id))
        .where(col(Scenario.deleted_at).is_(None))
        .with_for_update(of=Task)
        .execution_options(populate_existing=True)
    )
    task = (await session.execute(statement)).scalar_one_or_none()
    if task is None:
        raise NotFoundError(
            f"No restorable task {task_id} was deleted from scenario {scenario_id} within the restore window."
        )
    return task


async def restore_task(session: AsyncSession, task: Task) -> Task:
    """Clear ``task``'s tombstone.

    Nothing to repair: a task owns no ordering and no unique key, and the
    `TaskCompletion` rows pointing at it were never tombstoned — so the
    per-participant checkmarks reappear with it, which is the point of restoring
    rather than re-creating.
    """
    await restore_row(session, task, conflict_message="Task cannot be restored.")
    await session.refresh(task)
    return task


async def soft_delete_task(session: AsyncSession, task: Task, *, by_id: UUID) -> Task:
    """Soft-delete ``task`` by stamping `deleted_at`; subsequent reads exclude it."""
    task.soft_delete(by_id)
    session.add(task)
    await session.flush()
    await session.refresh(task)
    return task
