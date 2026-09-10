"""Scenario service — pure async functions over an `AsyncSession`.

Mirrors `services.assignments`: routers stay thin and raise `APIError`
subclasses; soft-deleted rows are filtered per-statement via the `live_*`
factories on `BaseModel`. Authorization is inherited one level down from the
parent group, exactly like the model-assignment sub-resource:

* **Reads** (`get_scenario`, `list_scenarios`) take a ``caller_id`` and resolve
  only scenarios whose evaluation and parent group are live and the group is
  `public`-or-member (`group_visible_to`). ``can_manage``
  (`evaluation_groups:manage`) lifts the public-or-member predicate, never the
  liveness requirement.
* **Writes** (`create`/`update`/`delete`/`reorder`) are gated at the route by
  `authorize_evaluation_mutation` (in-group role or break-glass), so the service
  functions here omit ``caller_id`` — the parent's write access has already been
  asserted.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col

from app.core.auth.roles import Permission
from app.core.evaluations.access import join_visible_evaluation_group
from app.core.evaluations.filters import ScenarioFilters
from app.core.evaluations.filters import ScenarioOrderBy
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.evaluations.schemas import ScenarioUpdateChanges
from app.core.evaluations.services.evaluation_groups import assert_group_write_access
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.ordering import apply_order_by
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row
from app.core.soft_delete import with_live


def _scope_to_visible(
    statement: Select[tuple[Scenario]], *, caller_id: UUID, can_manage: bool
) -> Select[tuple[Scenario]]:
    """Constrain a scenario `Select` to evaluations whose parent group is live and caller-visible.

    Mirrors `evaluations._scope_to_groups` one level deeper: a scenario resolves
    only when its evaluation and the parent group are live and the group is
    `public`-or-member. `can_manage` lifts the public-or-member predicate, never
    the liveness requirement.
    """
    return join_visible_evaluation_group(
        statement, col(Scenario.evaluation_id), caller_id=caller_id, can_manage=can_manage
    )


async def get_scenario(
    session: AsyncSession,
    evaluation_id: UUID,
    scenario_id: UUID,
    *,
    caller_id: UUID | None = None,
    can_manage: bool = False,
    for_update: bool = False,
    with_tasks: bool = False,
) -> Scenario:
    """Fetch the live scenario ``scenario_id`` belonging to ``evaluation_id``.

    The ``evaluation_id`` is part of the lookup, so an id from another
    evaluation resolves to nothing (a clean 404, not a cross-evaluation read).

    Pass ``caller_id`` on the **read** path to inherit the parent evaluation's
    visibility: the scenario resolves only when its evaluation and parent group
    are live and the group is `public`-or-member, so a caller who can't see the
    evaluation can't read its scenarios. ``can_manage`` lifts the public-or-member
    predicate but not liveness. Mutation paths omit ``caller_id`` — they authorize
    via `authorize_evaluation_mutation` and pass ``for_update=True`` to lock the row.

    ``with_tasks`` eager-loads the scenario's live ``tasks`` so the detail
    projection can embed them without a further (lazy, async-unsafe) load. Leave
    it off on mutation/existence paths that only need the row itself.

    Raises:
        NotFoundError: If no live scenario with ``scenario_id`` exists in the
            evaluation (or its parent isn't visible to ``caller_id`` when supplied).
    """
    statement = Scenario.live_select().where(
        col(Scenario.evaluation_id) == evaluation_id,
        col(Scenario.id) == scenario_id,
    )
    if caller_id is not None:
        statement = _scope_to_visible(statement, caller_id=caller_id, can_manage=can_manage)
    if with_tasks:
        statement = statement.options(selectinload(Scenario.tasks), with_live(Task))  # ty: ignore[invalid-argument-type]
    if for_update:
        statement = statement.with_for_update()
    scenario = (await session.execute(statement)).scalar_one_or_none()
    if scenario is None:
        raise NotFoundError(f"Scenario {scenario_id} not found in evaluation {evaluation_id}.")
    return scenario


async def get_scenario_by_id(
    session: AsyncSession, scenario_id: UUID, *, caller_id: UUID, can_manage: bool
) -> Scenario:
    """Fetch one live scenario by id alone, scoped to the caller's visibility.

    Unlike `get_scenario`, the evaluation id is not part of the key — used by routes
    that address a scenario directly (the nested tasks, the conversation creates).
    Resolves only when the scenario, its evaluation, and the parent group are live
    and the group is `public`-or-member (``can_manage`` lifts the visibility predicate).

    Raises:
        NotFoundError: If no live scenario ``scenario_id`` is visible to the caller.
    """
    statement = _scope_to_visible(
        Scenario.live_select().where(col(Scenario.id) == scenario_id),
        caller_id=caller_id,
        can_manage=can_manage,
    )
    scenario = (await session.execute(statement)).scalar_one_or_none()
    if scenario is None:
        raise NotFoundError(f"Scenario {scenario_id} not found.")
    return scenario


async def authorize_scenario_mutation(
    session: AsyncSession,
    scenario_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
) -> None:
    """Gate a write to ``scenario_id``'s sub-resources (its tasks) on the caller's object-scope authority.

    The scenario-level mirror of `authorize_evaluation_mutation`: resolves the
    parent group from the scenario id (joining scenario → evaluation → group) and
    applies the object-scope write gate (`assert_group_write_access`) for
    `evaluations:update` — so an in-group `owner` may write the scenario's tasks,
    while a member with a lesser role is denied even if they carry the permission
    globally. The lookup runs even under ``can_manage``, so existence and
    scenario/evaluation/group **liveness** are always enforced. The 404/403 split
    matches the group PATCH: a scenario under an invisible (private, no-membership)
    group reads as missing, a visible-but-insufficient one is forbidden.

    Raises:
        NotFoundError: If no live scenario ``scenario_id`` exists under a live
            evaluation and group, or its (non-public) group is not visible.
        ForbiddenError: If the group is visible but the caller's roles lack
            `evaluations:update` on it.
    """
    missing_message = f"Scenario {scenario_id} not found."
    group_id = await session.scalar(
        select(col(EvaluationGroup.id))
        .join(Evaluation, col(Evaluation.evaluation_group_id) == col(EvaluationGroup.id))
        .join(Scenario, col(Scenario.evaluation_id) == col(Evaluation.id))
        .where(col(Scenario.id) == scenario_id)
        .where(col(Scenario.deleted_at).is_(None))
        .where(col(Evaluation.deleted_at).is_(None))
        .where(col(EvaluationGroup.deleted_at).is_(None)),
    )
    if group_id is None:
        raise NotFoundError(missing_message)
    await assert_group_write_access(
        session,
        group_id=group_id,
        caller_id=caller_id,
        can_manage=can_manage,
        permission=Permission.EVALUATIONS_UPDATE,
        missing_message=missing_message,
    )


async def list_scenarios(
    session: AsyncSession,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    filters: ScenarioFilters,
    order_by: ScenarioOrderBy,
    limit: int,
    offset: int,
    deleted_cutoff: datetime,
) -> tuple[list[Scenario], int]:
    """Return one page of live scenarios visible to ``caller_id`` matching `filters`.

    Powers the standalone cross-evaluation `/scenarios` view; `filters.evaluation_id`
    narrows it to one evaluation (also reused by the nested list endpoint). The
    visibility scope is applied *before* the user filters so a filter can never
    widen it.

    ``filters.deleted`` swaps the live set for the tombstones inside the restore
    window, scoped to the caller's own deletes unless ``can_manage`` — like the
    evaluation list, this view spans groups, so it cannot run the per-group write
    gate the restore itself applies.
    ``deleted_cutoff`` comes from the route, like `get_restorable_scenario`'s — reaching for
    the global settings here would let the listing and the restore disagree on the window.
    """
    base = (
        deleted_select(Scenario, deleted_cutoff, deleted_by=None if can_manage else caller_id)
        if filters.deleted
        else Scenario.live_select()
    )
    statement = _scope_to_visible(base, caller_id=caller_id, can_manage=can_manage)
    if filters.evaluation_id is not None:
        statement = statement.where(col(Scenario.evaluation_id) == filters.evaluation_id)
    if filters.search is not None:
        pattern = f"%{filters.search}%"
        statement = statement.where(
            or_(
                col(Scenario.name).ilike(pattern, escape="\\"),
                col(Scenario.description).ilike(pattern, escape="\\"),
            ),
        )
    statement = apply_order_by(statement, Scenario, order_by)
    return await paginate(session, statement, limit=limit, offset=offset)


async def _next_position(session: AsyncSession, evaluation_id: UUID) -> int:
    """The position that appends after the evaluation's last live scenario.

    Aggregate (Core) path: `live_select`'s loader criteria does not apply, so
    soft-deleted rows are filtered by hand. The read takes no lock of its own, so
    **every caller must already hold the parent-evaluation row lock** — two callers
    that lock only their own scenario rows would both read the same `max(position)`
    and write the same value, and nothing enforces uniqueness there.
    """
    max_position = (
        await session.execute(
            select(func.max(col(Scenario.position))).where(
                col(Scenario.evaluation_id) == evaluation_id,
                col(Scenario.deleted_at).is_(None),
            ),
        )
    ).scalar_one_or_none()
    return 0 if max_position is None else max_position + 1


async def create_scenario(
    session: AsyncSession, *, evaluation_id: UUID, name: str, description: str, required_reviews: int = 1
) -> Scenario:
    """Create a scenario appended last (`position = max + 1`) in its evaluation.

    Locks the parent evaluation row (`for_update`) so concurrent creates on the
    same evaluation serialize: `position` has no DB uniqueness, so two unlocked
    creates could read the same `max(position)` and collide. The lookup also
    resolves the parent so a missing one surfaces as a clean 404 rather than an
    opaque FK violation (mirrors `assign_model`). The route has already asserted
    the caller's write access via `authorize_evaluation_mutation`.

    Raises:
        NotFoundError: If the evaluation does not exist (live).
    """
    await get_evaluation(session, evaluation_id, for_update=True, with_models=False)
    scenario = Scenario(
        evaluation_id=evaluation_id,
        name=name,
        description=description,
        position=await _next_position(session, evaluation_id),
        required_reviews=required_reviews,
    )
    session.add(scenario)
    await session.flush()
    await session.refresh(scenario, attribute_names=["created_at", "updated_at"])
    return scenario


async def update_scenario(session: AsyncSession, scenario: Scenario, changes: ScenarioUpdateChanges) -> Scenario:
    """Apply ``changes`` to ``scenario`` — writes only fields in `changes.model_fields_set`.

    Content-only (`name` / `description`): `evaluation_id` is immutable here and
    `position` is owned by reorder.
    """
    for field in changes.model_fields_set:
        setattr(scenario, field, getattr(changes, field))
    session.add(scenario)
    await session.flush()
    await session.refresh(scenario, attribute_names=["updated_at"])
    return scenario


async def soft_delete_scenario(session: AsyncSession, scenario: Scenario, *, by_id: UUID) -> Scenario:
    """Soft-delete ``scenario`` by stamping `deleted_at`.

    Leaves a gap in `position`; the next reorder re-densifies. (A delete that
    re-packs the remaining positions is intentionally not done — reorder owns
    that, and clients re-densify by sending the full set.)
    """
    scenario.soft_delete(by_id)
    session.add(scenario)
    await session.flush()
    await session.refresh(scenario)
    return scenario


async def get_restorable_scenario(
    session: AsyncSession,
    evaluation_id: UUID,
    scenario_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    deleted_cutoff: datetime,
) -> Scenario:
    """Fetch the tombstoned scenario of ``evaluation_id`` this caller may restore.

    An id from another evaluation, another actor's delete, one outside the window, or
    one whose parent evaluation/group is soft-deleted all read as missing — restoring
    under a dead parent would return a row no read can reach. The route authorizes the
    write via `authorize_evaluation_mutation` first, so no visibility predicate is
    applied here; the deleter predicate mirrors `list_scenarios`', so the restore
    reaches exactly the set the deleted listing offered.

    Raises:
        NotFoundError: If no such restorable scenario exists.
    """
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = (
        deleted_select(Scenario, deleted_cutoff, deleted_by=None if can_manage else caller_id)
        .where(col(Scenario.evaluation_id) == evaluation_id, col(Scenario.id) == scenario_id)
        .join(Evaluation, col(Scenario.evaluation_id) == col(Evaluation.id))
        .where(col(Evaluation.deleted_at).is_(None))
        .join(EvaluationGroup, col(Evaluation.evaluation_group_id) == col(EvaluationGroup.id))
        .where(col(EvaluationGroup.deleted_at).is_(None))
        .with_for_update(of=Scenario)
        .execution_options(populate_existing=True)
    )
    scenario = (await session.execute(statement)).scalar_one_or_none()
    if scenario is None:
        raise NotFoundError(
            f"No restorable scenario {scenario_id} was deleted from evaluation {evaluation_id} "
            f"within the restore window."
        )
    return scenario


async def restore_scenario(session: AsyncSession, scenario: Scenario) -> Scenario:
    """Clear ``scenario``'s tombstone, appending it last in its evaluation.

    It does **not** return to its old `position`: the delete left a gap there and a
    reorder since may have re-densified onto it, which would leave two live scenarios
    claiming one position (nothing enforces uniqueness — see `Scenario`). Appending
    mirrors `create_scenario` and keeps the order unambiguous; the client reorders
    afterwards if it wants the original slot back.

    Its tasks come back with it — the delete never tombstoned them, they hid behind
    the parent-liveness join (`tasks._scope_to_visible`).

    Takes the parent-evaluation row lock before reading `max(position)` — the same lock
    `create_scenario` takes, and for the same reason: the tombstone's own `FOR UPDATE`
    protects that row alone, so without this a concurrent restore or create in the same
    evaluation would read the same maximum and both append to it.
    """
    await get_evaluation(session, scenario.evaluation_id, for_update=True, with_models=False)
    scenario.position = await _next_position(session, scenario.evaluation_id)
    await restore_row(session, scenario, conflict_message="Scenario cannot be restored.")
    await session.refresh(scenario)
    return scenario


async def reorder_scenarios(session: AsyncSession, evaluation_id: UUID, scenario_ids: list[UUID]) -> list[Scenario]:
    """Rewrite `position` for every live scenario of ``evaluation_id`` to its index in ``scenario_ids``.

    ``scenario_ids`` must list the evaluation's live scenarios **exactly once**
    each (the schema already rejects duplicates). The live set is locked
    `FOR UPDATE` so concurrent reorders serialize. The route has already asserted
    the caller's write access via `authorize_evaluation_mutation`.

    The unknown-id check runs before the completeness check, so a payload that
    *both* names a foreign id and omits a real one surfaces as `NotFoundError`
    (404), not `ConflictError` (409) — a stray id is reported before the set is
    judged complete.

    Raises:
        NotFoundError: If any id is not a live scenario of the evaluation.
        ConflictError: If the set omits any of the evaluation's live scenarios.
    """
    rows = (
        (
            await session.execute(
                Scenario.live_select().where(col(Scenario.evaluation_id) == evaluation_id).with_for_update(),
            )
        )
        .scalars()
        .all()
    )
    by_id = {scenario.id: scenario for scenario in rows}
    requested = set(scenario_ids)
    unknown = requested - by_id.keys()
    if unknown:
        raise NotFoundError(
            f"Scenarios not found in evaluation {evaluation_id}: {sorted(str(uid) for uid in unknown)}."
        )
    if by_id.keys() - requested:
        raise ConflictError("scenario_ids must list every scenario in the evaluation exactly once.")
    for position, scenario_id in enumerate(scenario_ids):
        by_id[scenario_id].position = position
    session.add_all(rows)
    await session.flush()
    # Re-read ordered by the new position: the UPDATE bumps `updated_at`
    # server-side, expiring it, and an async response projection cannot lazy-load
    # an expired attribute (MissingGreenlet). One select reloads every column.
    return list(
        (
            await session.execute(
                Scenario.live_select()
                .where(col(Scenario.evaluation_id) == evaluation_id)
                .order_by(col(Scenario.position)),
            )
        )
        .scalars()
        .all(),
    )
