"""Evaluation model-assignment service — pure async functions over an `AsyncSession`.

Routers stay thin: they raise `APIError` subclasses for known failure modes and
let the error handler translate them into RFC 7807 responses. Soft-deleted rows
are filtered per-statement via the `live_*` factories on `BaseModel`.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import contains_eager
from sqlmodel import col

from app.core.ai_gateway import AiModel
from app.core.ai_gateway import get_model
from app.core.evaluations.access import group_visible_to
from app.core.evaluations.filters import EvaluationAiModelFilters
from app.core.evaluations.filters import EvaluationAiModelOrderBy
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.schemas import EvaluationAiModelUpdateChanges
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.ordering import apply_order_by
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row
from app.core.soft_delete import with_live


async def get_assignment(
    session: AsyncSession,
    evaluation_id: UUID,
    assignment_id: UUID,
    *,
    caller_id: UUID | None = None,
    can_manage: bool = False,
    for_update: bool = False,
) -> EvaluationAiModel:
    """Fetch the live assignment ``assignment_id`` belonging to ``evaluation_id``.

    Addressed by the assignment's own id (not the model's): the masked
    evaluation view never exposes the real `model_id`, and an assignment id —
    unlike `model_id` — doesn't correlate the same model across evaluations.
    The `evaluation_id` is part of the lookup, so an id from another evaluation
    resolves to nothing (a clean 404, not a cross-evaluation read).

    Pass ``caller_id`` on the **read** path to inherit the parent evaluation's
    visibility one level down: the assignment resolves only when its evaluation
    and parent group are live and the group is `public`-or-owned, so a caller who
    can't see the evaluation can't read its assignments (and so can't turn the
    masked view's `assignment_id` back into a real `model_id`). ``can_manage``
    lifts the public-or-owned predicate but not liveness. Mutation paths omit
    ``caller_id`` — they authorize via `authorize_evaluation_mutation` and pass
    ``for_update=True`` to lock the row.

    Raises:
        NotFoundError: If no live assignment with ``assignment_id`` exists in the
            evaluation (or its parent isn't visible to ``caller_id`` when supplied).
    """
    statement = EvaluationAiModel.live_select().where(
        col(EvaluationAiModel.evaluation_id) == evaluation_id,
        col(EvaluationAiModel.id) == assignment_id,
    )
    if caller_id is not None:
        statement = (
            statement.join(Evaluation, col(EvaluationAiModel.evaluation_id) == col(Evaluation.id))
            .where(col(Evaluation.deleted_at).is_(None))
            .join(EvaluationGroup, col(Evaluation.evaluation_group_id) == col(EvaluationGroup.id))
            .where(col(EvaluationGroup.deleted_at).is_(None))
        )
        if not can_manage:
            statement = statement.where(group_visible_to(caller_id))
    if for_update:
        statement = statement.with_for_update()
    assignment = (await session.execute(statement)).scalar_one_or_none()
    if assignment is None:
        raise NotFoundError(f"Assignment {assignment_id} not found in evaluation {evaluation_id}.")
    return assignment


def _name_column(masked: bool) -> Mapped[str | None]:
    """The column the listing surfaces as the model name.

    Masking-aware: a masked evaluation never exposes the real model name, so it
    reads `model_display_mask`; otherwise it reads the joined `AiModel.name`.
    """
    return col(EvaluationAiModel.model_display_mask) if masked else col(AiModel.name)


def _apply_model_search(
    statement: Select[tuple[EvaluationAiModel]], *, search: str, masked: bool
) -> Select[tuple[EvaluationAiModel]]:
    """Constrain to assignments whose surfaced name matches ``search``.

    ``search`` is already `escape_like`-sanitised at the filter edge.
    """
    return statement.where(_name_column(masked).ilike(f"%{search}%", escape="\\"))


def _apply_models_order_by(
    statement: Select[tuple[EvaluationAiModel]], order_by: EvaluationAiModelOrderBy, *, masked: bool
) -> Select[tuple[EvaluationAiModel]]:
    """Order by the masking-aware name, or delegate timestamp sorts to `apply_order_by`.

    `name` is not a column on `EvaluationAiModel` (it's `model_display_mask` or
    the joined `AiModel.name`), so it can't go through the generic helper;
    `created_at`/`updated_at` live on the assignment row and do. The assignment
    `id` is appended as a secondary key for deterministic pagination (matching
    `apply_order_by`).
    """
    if order_by in ("name", "-name"):
        target = _name_column(masked)
        primary = (target.desc() if order_by.startswith("-") else target.asc()).nulls_last()
        return statement.order_by(primary, col(EvaluationAiModel.id))
    return apply_order_by(statement, EvaluationAiModel, order_by)


async def list_evaluation_models(
    session: AsyncSession,
    *,
    evaluation_id: UUID,
    caller_id: UUID,
    filters: EvaluationAiModelFilters,
    order_by: EvaluationAiModelOrderBy,
    limit: int,
    offset: int,
    deleted_cutoff: datetime,
) -> tuple[list[EvaluationAiModel], int, bool]:
    """List one page of an evaluation's model assignments, masking-aware.

    Resolves the evaluation under the same visibility rule as the detail read
    (parent group public or caller-owned) — a non-visible evaluation surfaces as
    a 404. The returned mask flag (`mask_models_enabled`) drives the search/sort
    column selection here and the response projection at the route.

    `ai_model` is eager-loaded (joined) on every listing: the response projection
    reads `warmup_enabled` off the model regardless of masking
    (`EvaluationAiModelView.from_assignment`), and on the unmasked path search and
    sort additionally filter on `AiModel.name`. A masked listing sorts/searches on
    `model_display_mask`, so the join there only carries the row for the projection
    — but the load is still required (without it the masked projection lazy-loads
    on the async request session and raises `MissingGreenlet`).

    Returns:
        The page of assignments (with `ai_model` eager-loaded), the total count,
        and the evaluation's masking flag.

    Raises:
        NotFoundError: If the evaluation does not exist or is not visible.
    ``deleted_cutoff`` comes from the route, like `get_restorable_assignment`'s — reaching for
    the global settings here would let the listing and the restore disagree on the window.
    """
    evaluation = await get_evaluation(session, evaluation_id, caller_id=caller_id, with_models=False)
    masked = evaluation.mask_models_enabled
    base = (
        deleted_select(EvaluationAiModel, deleted_cutoff, deleted_by=None)
        if filters.deleted
        else EvaluationAiModel.live_select()
    )
    statement = (
        base.where(col(EvaluationAiModel.evaluation_id) == evaluation_id)
        .join(AiModel, col(EvaluationAiModel.model_id) == col(AiModel.id))
        .options(
            contains_eager(EvaluationAiModel.ai_model),  # ty: ignore[invalid-argument-type]
            with_live(AiModel),
        )
    )
    if filters.deleted:
        # Redundant but deliberate: `with_live(AiModel)` above is `with_loader_criteria`,
        # which SQLAlchemy folds into this inner join's ON clause, so a dead-model row is
        # already excluded. Repeating it here states the *rule* locally — a tombstoned
        # assignment whose model is gone is not restorable (it could never dispatch), so
        # the restorable set must not depend on a loader option someone may change.
        # A live assignment always has a live model anyway (the model-delete cascade
        # unassigns them), which is why the live branch needs nothing.
        statement = statement.where(col(AiModel.deleted_at).is_(None))
    if filters.search is not None:
        statement = _apply_model_search(statement, search=filters.search, masked=masked)
    statement = _apply_models_order_by(statement, order_by, masked=masked)
    items, total = await paginate(session, statement, limit=limit, offset=offset)
    return items, total, masked


async def assign_model(
    session: AsyncSession,
    *,
    evaluation_id: UUID,
    model_id: UUID,
    model_display_mask: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> EvaluationAiModel:
    """Assign an existing model to an existing evaluation.

    Both the evaluation and the model are resolved first, so a missing parent
    surfaces as a clean 404 rather than an opaque FK violation; the unique
    `(model_id, evaluation_id)` index turns a re-assignment into a 409.

    Args:
        session: Async DB session bound to the request.
        evaluation_id: Evaluation to attach the model to.
        model_id: Model being assigned. Must exist and be live.
        model_display_mask: Optional alias shown when the evaluation masks models.
        parameters: Per-assignment inference-param overrides (JSONB).

    Raises:
        NotFoundError: If the evaluation or the model does not exist (live).
        ConflictError: If the model is already assigned to this evaluation.
    """
    await get_evaluation(session, evaluation_id, with_models=False)
    await get_model(session, model_id)
    assignment = EvaluationAiModel(
        evaluation_id=evaluation_id,
        model_id=model_id,
        model_display_mask=model_display_mask,
        parameters=dict(parameters) if parameters is not None else {},
    )
    session.add(assignment)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError("Model is already assigned to this evaluation.") from exc
    await session.refresh(assignment, attribute_names=["created_at", "updated_at"])
    return assignment


async def update_assignment(
    session: AsyncSession, assignment: EvaluationAiModel, changes: EvaluationAiModelUpdateChanges
) -> EvaluationAiModel:
    """Apply ``changes`` to ``assignment`` and persist them.

    Writes only fields in `changes.model_fields_set` — omitted fields stay,
    explicit `None` clears the nullable `model_display_mask`.
    """
    for field in changes.model_fields_set:
        setattr(assignment, field, getattr(changes, field))
    session.add(assignment)
    await session.flush()
    await session.refresh(assignment, attribute_names=["updated_at"])
    return assignment


async def soft_delete_assignment(
    session: AsyncSession, assignment: EvaluationAiModel, *, by_id: UUID
) -> EvaluationAiModel:
    """Unassign a model by stamping `deleted_at` on the assignment row."""
    assignment.soft_delete(by_id)
    session.add(assignment)
    await session.flush()
    await session.refresh(assignment)
    return assignment


async def get_restorable_assignment(
    session: AsyncSession, evaluation_id: UUID, assignment_id: UUID, *, deleted_cutoff: datetime
) -> EvaluationAiModel:
    """Fetch the tombstoned assignment a caller may re-instate.

    `get_assignment`'s lookup over the restorable tombstones: an id from another
    evaluation, one outside the window, or one whose **model** has since been deleted
    all read as missing — the last because restoring it would hand the evaluation an
    assignment that can never dispatch. The caller authorizes through
    `authorize_evaluation_mutation` first, as the delete path does, so no visibility
    predicate is applied here.

    Raises:
        NotFoundError: If no such restorable assignment exists.
    """
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = (
        deleted_select(EvaluationAiModel, deleted_cutoff, deleted_by=None)
        .where(
            col(EvaluationAiModel.evaluation_id) == evaluation_id,
            col(EvaluationAiModel.id) == assignment_id,
        )
        .join(AiModel, col(EvaluationAiModel.model_id) == col(AiModel.id))
        .where(col(AiModel.deleted_at).is_(None))
        .with_for_update(of=EvaluationAiModel)
        .execution_options(populate_existing=True)
    )
    assignment = (await session.execute(statement)).scalar_one_or_none()
    if assignment is None:
        raise NotFoundError(
            f"No restorable assignment {assignment_id} was unassigned from evaluation {evaluation_id} "
            f"within the restore window."
        )
    return assignment


async def restore_assignment(session: AsyncSession, assignment: EvaluationAiModel) -> EvaluationAiModel:
    """Re-instate ``assignment``, making its model dispatchable in the evaluation again.

    Restoring the assignment also makes the conversations its removal tombstoned
    restorable again — they are gated on a *live* assignment
    (`conversations._join_live_assignment`), not on a flag of their own, so the
    property is derived and comes back with the assignment. That is the intended
    reading: the model can run again, so its conversations can too. They stay deleted
    until each is restored.

    Raises:
        ConflictError: If the model has since been re-assigned to this evaluation —
            `(model_id, evaluation_id)` is unique among live rows.
    """
    clash = await session.scalar(
        EvaluationAiModel.live_select()
        .where(
            col(EvaluationAiModel.evaluation_id) == assignment.evaluation_id,
            col(EvaluationAiModel.model_id) == assignment.model_id,
        )
        .limit(1)
    )
    if clash is not None:
        raise ConflictError("This model is already assigned to the evaluation; remove the newer assignment first.")
    await restore_row(session, assignment, conflict_message="Assignment cannot be restored.")
    await session.refresh(assignment)
    return assignment


async def unassign_models_for_model(session: AsyncSession, model_id: UUID, *, by_id: UUID) -> int:
    """Soft-delete every live assignment of ``model_id``, returning the count.

    Cascade hook for `AiModel` soft-delete: keeps the invariant that a live
    assignment always points at a live model, so evaluation reads never surface
    a tombstoned model. `live_update` scopes the bulk `UPDATE` to live rows, so
    already-unassigned rows are untouched and the call is idempotent.
    """
    statement = (
        EvaluationAiModel.live_update()
        .where(col(EvaluationAiModel.model_id) == model_id)
        .values(deleted_at=func.now(), deleted_by_id=by_id)
    )
    result = await session.execute(statement)
    return result.rowcount  # ty: ignore[unresolved-attribute]  # CursorResult at runtime; execute() is typed as Result
