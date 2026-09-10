"""Evaluation-group allowed-model-subset service — pure async functions over an `AsyncSession`.

A group's subset (`EvaluationGroupAiModel` rows) is the set of models its
evaluations may be assigned (`assert_model_assignable_to_group`). It is written
only through `sync_group_models` — the declarative `allowed_model_ids` field on
the group create/edit forms — so there is no per-row CRUD surface. An empty
subset is an *incomplete* state (a draft, a legacy group, or one emptied by a
model-delete cascade), never "everything allowed": assignability fails closed,
otherwise deleting a model could silently widen a group's pool. Callers resolve
the group; these functions trust ``group_id``. Soft-deleted rows are filtered
per-statement via the `live_*` factories on `BaseModel`.
"""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import ColumnElement
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway import AiModel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroupAiModel
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError


async def _model_used_by_group_evaluations(session: AsyncSession, *, group_id: UUID, model_id: UUID) -> bool:
    """Whether ``model_id`` is assigned to any live evaluation in ``group_id``."""
    statement = (
        select(col(EvaluationAiModel.id))
        .join(Evaluation, col(EvaluationAiModel.evaluation_id) == col(Evaluation.id))
        .where(
            col(Evaluation.evaluation_group_id) == group_id,
            col(EvaluationAiModel.model_id) == model_id,
            col(EvaluationAiModel.deleted_at).is_(None),
            col(Evaluation.deleted_at).is_(None),
        )
        .limit(1)
    )
    return (await session.execute(statement)).first() is not None


async def _assert_models_live(session: AsyncSession, model_ids: set[UUID]) -> None:
    """Raise `BadRequestError` naming every id that is not a live model."""
    if not model_ids:
        return
    result = await session.execute(
        select(col(AiModel.id)).where(col(AiModel.id).in_(model_ids), col(AiModel.deleted_at).is_(None))
    )
    missing = model_ids - set(result.scalars())
    if missing:
        listed = ", ".join(str(model_id) for model_id in sorted(missing))
        raise BadRequestError(f"allowed_model_ids contains unknown model id(s): {listed}.")


async def sync_group_models(
    session: AsyncSession, *, group_id: UUID, model_ids: Collection[UUID], by_id: UUID
) -> tuple[list[UUID], list[UUID]]:
    """Replace ``group_id``'s allowed-model subset with ``model_ids`` (a declarative set).

    Duplicates in the input collapse. Every id must reference a live model;
    a removal is forbidden while its model is assigned to a live evaluation in
    the group. Current rows are locked `FOR UPDATE`, so a concurrent
    `assert_model_assignable_to_group` (`FOR SHARE`) serializes against the
    removal — no window to assign a just-removed model or remove a just-used one.

    Adding to an *empty* subset locks no rows here, so a caller editing an existing
    group must already hold that group's `evaluation_groups` row `FOR UPDATE` to
    serialize concurrent syncs — otherwise two adds of the same model race to the
    partial-unique index and the loser raises an uncaught `IntegrityError`. The PATCH
    route holds it (`get_evaluation_group(..., for_update=True)`); create/draft own a
    fresh, uncontended group id.

    Returns:
        ``(previous_ids, current_ids)``, each sorted.

    Raises:
        BadRequestError: If any id does not reference a live model.
        ConflictError: If a removed model is assigned to a live evaluation in the group.
    """
    target = set(model_ids)
    await _assert_models_live(session, target)
    current_rows = (
        (
            await session.execute(
                EvaluationGroupAiModel.live_select()
                .where(col(EvaluationGroupAiModel.evaluation_group_id) == group_id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    previous = {row.model_id for row in current_rows}
    for row in current_rows:
        if row.model_id in target:
            continue
        if await _model_used_by_group_evaluations(session, group_id=group_id, model_id=row.model_id):
            raise ConflictError("Cannot remove a model that is assigned to an evaluation in this group.")
        row.soft_delete(by_id)
        session.add(row)
    for model_id in target - previous:
        session.add(EvaluationGroupAiModel(evaluation_group_id=group_id, model_id=model_id))
    await session.flush()
    return sorted(previous), sorted(target)


async def unassign_group_models_for_model(session: AsyncSession, model_id: UUID, *, by_id: UUID) -> int:
    """Soft-delete every live subset row referencing ``model_id``, returning the count.

    Cascade hook for `AiModel` soft-delete (mirrors `unassign_models_for_model`):
    keeps a live subset row always pointing at a live model. May empty a group's
    subset — assignability then fails closed until it is reconfigured.
    """
    statement = (
        EvaluationGroupAiModel.live_update()
        .where(col(EvaluationGroupAiModel.model_id) == model_id)
        .values(deleted_at=func.now(), deleted_by_id=by_id)
    )
    result = await session.execute(statement)
    return result.rowcount  # ty: ignore[unresolved-attribute]  # CursorResult at runtime; execute() is typed as Result


async def assert_model_assignable_to_group(session: AsyncSession, *, group_id: UUID, model_id: UUID) -> None:
    """Gate assigning ``model_id`` to an evaluation on the group's allowed-model subset.

    Fails closed on an empty subset (see the module docstring). The subset rows
    are read `FOR SHARE`, so a concurrent `sync_group_models` removal
    (`FOR UPDATE`) serializes against this check.

    Raises:
        BadRequestError: If the subset is empty or does not contain the model.
    """
    rows = (
        (
            await session.execute(
                EvaluationGroupAiModel.live_select()
                .where(col(EvaluationGroupAiModel.evaluation_group_id) == group_id)
                .with_for_update(read=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise BadRequestError("The evaluation group has no allowed models configured.")
    if all(row.model_id != model_id for row in rows):
        raise BadRequestError("Model is not in the evaluation group's allowed-model subset.")


def assignable_model_conditions(evaluation: Evaluation) -> list[ColumnElement[bool]]:
    """WHERE conditions constraining `AiModel` rows to those assignable to ``evaluation``.

    Powers the `assignable_to_evaluation` filter on `GET /ai-models`: a model is
    assignable when it is in the evaluation's parent group's allowed-model subset
    (an empty subset yields none — fail-closed, matching `assert_model_assignable_to_group`)
    and is not already assigned to the evaluation. Returns conditions the caller
    applies to its own `AiModel` select, so the ai-gateway service stays ignorant
    of the evaluation tables (the cross-domain join lives here). The caller resolves
    ``evaluation`` via `get_evaluation`, which enforces read-visibility of the parent
    group — so this filter can't leak the subset of a group the caller can't see.
    """
    allowed = select(col(EvaluationGroupAiModel.model_id)).where(
        col(EvaluationGroupAiModel.evaluation_group_id) == evaluation.evaluation_group_id,
        col(EvaluationGroupAiModel.deleted_at).is_(None),
    )
    assigned = select(col(EvaluationAiModel.model_id)).where(
        col(EvaluationAiModel.evaluation_id) == evaluation.id,
        col(EvaluationAiModel.deleted_at).is_(None),
    )
    return [col(AiModel.id).in_(allowed), col(AiModel.id).notin_(assigned)]
