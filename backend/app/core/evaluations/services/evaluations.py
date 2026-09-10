"""Evaluation CRUD services — pure async functions over an `AsyncSession`.

Authorization is inherited one level down from the parent group, mirroring the
role-or-break-glass shape `evaluation_groups` edits apply:

* **Reads** (`list_evaluations`, `get_evaluation` given a `caller_id`) enforce
  visibility — an evaluation is visible only when its parent group is live and
  `public`-or-member. The `evaluation_groups:manage` elevation (`can_manage`)
  lifts the `public`-or-member predicate so a manager resolves any evaluation, but
  never the parent-group liveness requirement.
* **Writes** (`create_evaluation`, plus `authorize_evaluation_mutation` guarding
  `update`/`soft_delete` and the assignment sub-resource) require an in-group
  role granting the operation's permission on the parent group, or the
  break-glass `evaluation_groups:manage`. Public visibility grants reads, never
  writes; an invisible (private, no membership) parent reads as missing (404) so
  the gate never leaks a private group's existence, while a *visible* group where
  the caller's roles lack the permission yields 403 — the same 404-then-403 split
  the group PATCH applies.

Every read that projects models eager-loads each assignment's `ai_model` so the
response projection can mask or surface model identities without further DB
access — the standalone reads here via `_with_loaded_models`, and the
group-detail embed via the nested load in `services.evaluation_groups`.
"""

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col

from app.core.auth.roles import Permission
from app.core.evaluations.access import group_visible_to
from app.core.evaluations.enums import GROUP_STATUSES_ACCEPTING_EVALUATIONS
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.filters import EvaluationFilters
from app.core.evaluations.filters import EvaluationOrderBy
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import EvaluationTagKey
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.evaluations.schemas import EvaluationUpdate
from app.core.evaluations.services.evaluation_groups import assert_group_write_access
from app.core.evaluations.services.loaders import evaluation_models_loader
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.licenses.catalog import effective_license
from app.core.licenses.models import DataLicense
from app.core.licenses.models import defer_license_text
from app.core.licenses.service import validate_license_ref
from app.core.ordering import apply_order_by
from app.core.pagination import paginate
from app.core.platform_settings.service import get_platform_settings
from app.core.restore import deleted_select
from app.core.restore import restore_row
from app.core.soft_delete import with_live


def _assert_group_accepts_evaluations(group_id: UUID, group_status: PublicationStatus) -> None:
    if group_status not in GROUP_STATUSES_ACCEPTING_EVALUATIONS:
        raise ConflictError(
            f"Evaluation group {group_id} is '{group_status}'; "
            "evaluations can be added only to an approved or published group."
        )


def _with_loaded_models(statement: Select[tuple[Evaluation]]) -> Select[tuple[Evaluation]]:
    """Eager-load live assignments and each one's `ai_model`.

    `with_live(EvaluationAiModel)` drops soft-deleted assignments; the model
    itself is loaded regardless of its own `deleted_at` (defense-in-depth — the
    delete cascade keeps it live, but the projection must never NPE if not).
    """
    return statement.options(evaluation_models_loader(), with_live(EvaluationAiModel))


def _scope_to_groups(
    statement: Select[tuple[Evaluation]], *, caller_id: UUID, can_manage: bool
) -> Select[tuple[Evaluation]]:
    """Join the live parent group and, unless `can_manage`, constrain to caller-visible groups.

    Parent-group **liveness** is always enforced — a soft-deleted group hides its
    evaluations from everyone, managers included. `can_manage` lifts only the
    `public`-or-member visibility predicate, never the requirement that the group
    exist and be live.
    """
    statement = statement.join(EvaluationGroup, col(Evaluation.evaluation_group_id) == col(EvaluationGroup.id)).where(
        col(EvaluationGroup.deleted_at).is_(None)
    )
    if not can_manage:
        statement = statement.where(group_visible_to(caller_id))
    return statement


async def authorize_evaluation_mutation(
    session: AsyncSession,
    evaluation_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    permission: Permission,
    allow_deleted: bool = False,
) -> tuple[UUID, PublicationStatus]:
    """Gate a write to the evaluation ``evaluation_id`` (or its assignment sub-resource).

    Resolves the parent group from the evaluation id and applies the object-scope
    write gate (`assert_group_write_access`) for ``permission`` — so an in-group
    `owner` may write the group's evaluations, while a member with a lesser role
    is denied even if they carry the permission globally. The lookup runs even
    under `can_manage`, so existence and parent-group **liveness** are always
    enforced — a manager may write across owners, but never to an evaluation that
    is gone or whose group is soft-deleted. The 404/403 split matches the group
    PATCH: an evaluation under an invisible (private, unrelated) group reads as
    missing, while a visible-but-insufficient one is forbidden.

    ``allow_deleted`` drops the evaluation-liveness predicate for the restore path,
    whose subject is a tombstone by definition — the parent group must still be live
    either way. Every other caller leaves it False, so a deleted evaluation stays
    unwritable.

    Returns:
        ``(group_id, status)`` for the parent group, both already fetched by the
        gate's lookup — so the assign path can gate the allowed-model subset and the
        duplicate path can enforce the lifecycle allowlist without a second query.
        Most callers ignore it.

    Raises:
        NotFoundError: If no live evaluation ``evaluation_id`` exists under a live
            group, or its (non-public) parent group is not visible to the caller.
        ForbiddenError: If the parent group is visible but the caller lacks
            ``permission`` on it.
    """
    missing_message = f"Evaluation {evaluation_id} not found."
    statement = (
        select(col(EvaluationGroup.id), col(EvaluationGroup.status))
        .join(Evaluation, col(Evaluation.evaluation_group_id) == col(EvaluationGroup.id))
        .where(col(Evaluation.id) == evaluation_id)
        .where(col(EvaluationGroup.deleted_at).is_(None))
    )
    if not allow_deleted:
        statement = statement.where(col(Evaluation.deleted_at).is_(None))
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        raise NotFoundError(missing_message)
    group_id, group_status = row
    await assert_group_write_access(
        session,
        group_id=group_id,
        caller_id=caller_id,
        can_manage=can_manage,
        permission=permission,
        missing_message=missing_message,
    )
    return group_id, group_status


async def get_evaluation(
    session: AsyncSession,
    evaluation_id: UUID,
    *,
    caller_id: UUID | None = None,
    can_manage: bool = False,
    for_update: bool = False,
    with_models: bool = True,
) -> Evaluation:
    """Fetch one live evaluation by id.

    Pass ``caller_id`` on read paths to enforce visibility: the parent group
    must be live and public-or-owned, else the row reads as missing (no existence
    leak). ``can_manage`` (`evaluation_groups:manage`) lifts the public-or-owned
    predicate so a manager resolves any evaluation — but the parent group must
    still be live. Mutation paths gate access via `authorize_evaluation_mutation`
    instead, so they omit ``caller_id`` and pass ``for_update=True`` to lock the
    row for a same-transaction write.

    ``with_models`` eager-loads each live assignment and its ``ai_model`` so the
    response projection can mask or surface identities without further DB
    access. Set it to ``False`` on paths that only need the row itself — an
    existence check, a delete, or listing the assignments separately (where
    eager-loading the whole collection would defeat pagination).

    Raises:
        NotFoundError: If no live row matches ``evaluation_id`` (or its parent
            group is soft-deleted, or not visible to ``caller_id`` when supplied
            without ``can_manage``).
    """
    statement = Evaluation.live_select().where(col(Evaluation.id) == evaluation_id)
    if caller_id is not None:
        statement = _scope_to_groups(statement, caller_id=caller_id, can_manage=can_manage)
    if with_models:
        statement = _with_loaded_models(statement)
    if for_update:
        statement = statement.with_for_update()
    evaluation = (await session.execute(statement)).scalar_one_or_none()
    if evaluation is None:
        raise NotFoundError(f"Evaluation {evaluation_id} not found.")
    return evaluation


def _apply_evaluation_filters(
    statement: Select[tuple[Evaluation]], filters: EvaluationFilters
) -> Select[tuple[Evaluation]]:
    """Append a WHERE clause for every supplied filter, leave omitted ones alone."""
    if filters.evaluation_group_id is not None:
        statement = statement.where(col(Evaluation.evaluation_group_id) == filters.evaluation_group_id)
    if filters.status is not None:
        statement = statement.where(col(Evaluation.status) == filters.status)
    if filters.search is not None:
        pattern = f"%{filters.search}%"
        statement = statement.where(
            or_(
                col(Evaluation.title).ilike(pattern, escape="\\"),
                col(Evaluation.description).ilike(pattern, escape="\\"),
            ),
        )
    return statement


async def list_evaluations(
    session: AsyncSession,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    filters: EvaluationFilters,
    order_by: EvaluationOrderBy,
    limit: int,
    offset: int,
    deleted_cutoff: datetime,
) -> tuple[list[Evaluation], int]:
    """Return one page of evaluations visible to ``caller_id`` matching ``filters``.

    The visibility scope (live parent group that is `public` or caller-owned) is
    applied *before* the user filters so a filter can never widen it. ``can_manage``
    (`evaluation_groups:manage`) lifts the public-or-owned predicate to every
    evaluation, but evaluations under a soft-deleted group stay hidden from
    everyone.

    ``filters.deleted`` swaps the live set for the tombstones inside the restore
    window. Unlike the nested assignment listing — which authorizes against its one
    parent group — this list spans groups, so it is scoped to the caller's **own**
    deletes unless ``can_manage``: every row it returns is one the caller can
    actually restore, rather than one whose restore would 403 on the group gate.
    ``deleted_cutoff`` comes from the route, like `get_restorable_evaluation`'s — reaching for
    the global settings here would let the listing and the restore disagree on the window.
    """
    base = (
        deleted_select(Evaluation, deleted_cutoff, deleted_by=None if can_manage else caller_id)
        if filters.deleted
        else Evaluation.live_select()
    )
    statement = _scope_to_groups(base, caller_id=caller_id, can_manage=can_manage)
    statement = _apply_evaluation_filters(statement, filters)
    statement = apply_order_by(statement, Evaluation, order_by)
    statement = _with_loaded_models(statement)
    return await paginate(session, statement, limit=limit, offset=offset)


async def create_evaluation(  # noqa: PLR0913 — keyword-only args mirror the evaluation's column set; a Pydantic carrier would just shift the surface area
    session: AsyncSession,
    *,
    title: str,
    description: str | None = None,
    evaluation_group_id: UUID,
    created_by_id: UUID,
    can_manage: bool = False,
    cover_image: str | None = None,
    mask_models_enabled: bool = True,
    tags_enabled: bool = True,
    tags_restricted: bool = False,
    data_license_id: UUID | None = None,
) -> Evaluation:
    """Create a draft evaluation under an existing group the caller can write.

    The parent group is resolved first so a missing group surfaces as a clean
    404 rather than an opaque FK violation, then the object-scope write gate
    is applied (`assert_group_write_access`): only a holder of an in-group role
    granting `evaluations:create` (e.g. `owner`) — or of `evaluation_groups:manage`
    (``can_manage``) — may add evaluations to it. Authority alone is not enough:
    the group must also be in a state accepting evaluations
    (`GROUP_STATUSES_ACCEPTING_EVALUATIONS`), checked after the write gate so the
    404/403 split stays authoritative and the 409 never leaks a group the caller
    couldn't write anyway. ``status`` is left to its `new` default — lifecycle
    transitions are a separate concern. ``created_by_id`` records the authoring
    caller (attribution only).

    Raises:
        NotFoundError: If ``evaluation_group_id`` matches no live group, or the
            (non-public) group is not visible to the caller.
        ForbiddenError: If the caller can see the group but lacks the permission
            on it.
        ConflictError: If the group is not approved or published.
    """
    group = (
        await session.execute(EvaluationGroup.live_select().where(col(EvaluationGroup.id) == evaluation_group_id))
    ).scalar_one_or_none()
    if group is None:
        raise NotFoundError(f"Evaluation group {evaluation_group_id} not found.")
    await assert_group_write_access(
        session,
        group_id=group.id,
        caller_id=created_by_id,
        can_manage=can_manage,
        permission=Permission.EVALUATIONS_CREATE,
        missing_message=f"Evaluation group {evaluation_group_id} not found.",
    )
    _assert_group_accepts_evaluations(group.id, group.status)
    if data_license_id is not None:
        await validate_license_ref(session, data_license_id)

    evaluation = Evaluation(
        title=title,
        description=description,
        evaluation_group_id=evaluation_group_id,
        created_by_id=created_by_id,
        cover_image=cover_image,
        mask_models_enabled=mask_models_enabled,
        tags_enabled=tags_enabled,
        tags_restricted=tags_restricted,
        data_license_id=data_license_id,
    )
    session.add(evaluation)
    await session.flush()
    # Load `models` (empty for a fresh row) so the response projection doesn't
    # trip an async lazy-load when it iterates the relationship.
    await session.refresh(evaluation, attribute_names=["created_at", "updated_at", "status", "models"])
    return evaluation


async def duplicate_evaluation(
    session: AsyncSession,
    *,
    source_evaluation_id: UUID,
    caller_id: UUID,
    can_manage: bool,
    include_children: bool = False,
) -> Evaluation:
    """Copy an evaluation into a fresh `new` row in the same group, owned by the caller.

    Always copies the evaluation's own fields (title/description/mask/cover/license) and the admin
    tag schema — both flags plus the allowed keys, since the flag alone would forbid every tag.
    With ``include_children`` it also deep-copies the model assignments and the
    scenarios + tasks — referencing the same shared `AiModel` rows (no secrets: the
    API key lives on the `AiModel`, untouched), skipping any assignment whose model is
    soft-deleted so the copy can't resurrect a dead reference. Runtime data is never
    copied: conversations, messages, flags, reviews. The copy starts in `new` — the
    evaluation has no draft/publish gate (unlike a group, which duplicates into `draft`).

    Duplicating needs write access on the source — the same object-scope gate as a
    create (`authorize_evaluation_mutation` for `evaluations:create`): an in-group role
    on the parent group granting that permission (the creator holds `owner`), or the
    break-glass `evaluation_groups:manage`. A caller who can merely *see* the source
    cannot. The copy lands in the source's own group, so the group must also accept
    evaluations (`GROUP_STATUSES_ACCEPTING_EVALUATIONS`) — otherwise duplication would
    bypass the create path's lifecycle gate (e.g. growing a duplicated `draft` or a
    finished `inactive` group).

    Raises:
        NotFoundError: If no live source evaluation matches ``source_evaluation_id``
            under a live group visible to the caller.
        ForbiddenError: If the caller can see the source but lacks write access on it.
        ConflictError: If the parent group is not approved or published.
    """
    _, group_status = await authorize_evaluation_mutation(
        session,
        source_evaluation_id,
        caller_id=caller_id,
        can_manage=can_manage,
        permission=Permission.EVALUATIONS_CREATE,
    )
    source = await get_evaluation(session, source_evaluation_id, with_models=include_children)
    _assert_group_accepts_evaluations(source.evaluation_group_id, group_status)

    copy = Evaluation(
        title=source.title,
        description=source.description,
        mask_models_enabled=source.mask_models_enabled,
        cover_image=source.cover_image,
        data_license_id=source.data_license_id,
        created_by_id=caller_id,
        evaluation_group_id=source.evaluation_group_id,
    )
    session.add(copy)
    if include_children:
        # Every PK is a client-side UUID (BaseModel.id default_factory), so `copy.id`
        # is available before flush — build the whole graph and let one trailing flush
        # insert it in FK order, mirroring `duplicate_evaluation_group`.
        for src_assignment in source.models:
            if src_assignment.ai_model is not None and src_assignment.ai_model.deleted_at is not None:
                continue
            session.add(
                EvaluationAiModel(
                    evaluation_id=copy.id,
                    model_id=src_assignment.model_id,
                    model_display_mask=src_assignment.model_display_mask,
                    parameters=dict(src_assignment.parameters),
                )
            )
        scenarios = (
            (
                await session.execute(
                    Scenario.live_select()
                    .where(col(Scenario.evaluation_id) == source.id)
                    .options(selectinload(Scenario.tasks), with_live(Task))  # ty: ignore[invalid-argument-type]
                    .order_by(col(Scenario.position))
                )
            )
            .scalars()
            .all()
        )
        for src_scenario in scenarios:
            new_scenario = Scenario(
                name=src_scenario.name,
                description=src_scenario.description,
                position=src_scenario.position,  # preserve verbatim (don't renumber)
                required_reviews=src_scenario.required_reviews,
                evaluation_id=copy.id,
            )
            session.add(new_scenario)
            for src_task in src_scenario.tasks:
                session.add(Task(name=src_task.name, description=src_task.description, scenario_id=new_scenario.id))
    # The admin tag schema travels with the copy whether or not children are included: it is
    # governance, not child data, so a shell duplicate must not silently drop the restriction —
    # and the flag alone would forbid every tag, so the flag and its keys move together.
    copy.tags_enabled = source.tags_enabled
    copy.tags_restricted = source.tags_restricted
    source_tag_keys = (
        (await session.execute(EvaluationTagKey.live_select().where(col(EvaluationTagKey.evaluation_id) == source.id)))
        .scalars()
        .all()
    )
    for src_tag_key in source_tag_keys:
        session.add(EvaluationTagKey(evaluation_id=copy.id, key=src_tag_key.key))

    await session.flush()
    # Re-fetch with `models` (+ each assignment's `ai_model`) eager-loaded so the
    # response projection masks/surfaces identities without an async lazy-load.
    return await get_evaluation(session, copy.id, with_models=True)


async def update_evaluation(session: AsyncSession, evaluation: Evaluation, changes: EvaluationUpdate) -> Evaluation:
    """Apply ``changes`` to ``evaluation`` and persist them.

    Writes only fields in `changes.model_fields_set` — omitted fields stay. An explicit
    `None` clears a nullable field: `description`, `cover_image`, or `data_license_id` (which
    resets the override so the evaluation inherits its group's license / the platform default again).
    deliberately excluded from `EvaluationUpdate._reject_explicit_null` for exactly this reason.
    """
    if "data_license_id" in changes.model_fields_set and changes.data_license_id is not None:
        await validate_license_ref(session, changes.data_license_id)
    for field in changes.model_fields_set:
        setattr(evaluation, field, getattr(changes, field))
    session.add(evaluation)
    await session.flush()
    await session.refresh(evaluation, attribute_names=["updated_at"])
    return evaluation


async def resolve_effective_licenses(session: AsyncSession, evaluation_ids: Iterable[UUID]) -> dict[UUID, DataLicense]:
    """Map each requested evaluation id to its effective data-license row.

    Cascade (first set wins): evaluation override → group override → platform default. One
    platform-default read, one batched query joining each evaluation's parent group for the override
    layer, then one batched load of the distinct effective licence rows (no N+1). The result contains
    every requested id — an id whose evaluation is missing or soft-deleted maps to the platform
    default, so callers index it directly. The group join and the licence load ignore `deleted_at`,
    so a soft-deleted parent group or licence still resolves for rows referencing it; a soft-deleted
    evaluation is excluded and falls back to the platform default.
    """
    ids = set(evaluation_ids)
    if not ids:  # guard before the platform-default read — empty pages cost no query
        return {}
    default_id = (await get_platform_settings(session)).default_license_id
    rows = (
        await session.execute(
            select(col(Evaluation.id), col(Evaluation.data_license_id), col(EvaluationGroup.data_license_id))
            .join(EvaluationGroup, col(Evaluation.evaluation_group_id) == col(EvaluationGroup.id))
            .where(col(Evaluation.id).in_(ids), col(Evaluation.deleted_at).is_(None))
        )
    ).all()
    overrides = {row[0]: (row[1], row[2]) for row in rows}
    effective_ids = {
        evaluation_id: effective_license(*overrides.get(evaluation_id, (None, None)), default=default_id)
        for evaluation_id in ids
    }
    # Batch-load the distinct effective licence rows (plain select — a soft-deleted licence still
    # resolves for rows that reference it, so lineage doesn't lapse). The projections built from these
    # rows carry `has_content`, never the text, and this query backs every evaluation and conversation
    # list.
    licenses = (
        (
            await session.execute(
                select(DataLicense)
                .options(defer_license_text())
                .where(col(DataLicense.id).in_(set(effective_ids.values())))
            )
        )
        .scalars()
        .all()
    )
    by_id = {lic.id: lic for lic in licenses}
    resolved: dict[UUID, DataLicense] = {}
    for evaluation_id, license_id in effective_ids.items():
        lic = by_id.get(license_id)
        if lic is None:  # a referenced licence row is gone — most often the platform default (sync_licenses not run)
            raise RuntimeError(f"Data license {license_id} is missing; run synclicenses.")
        resolved[evaluation_id] = lic
    return resolved


async def resolve_effective_license(session: AsyncSession, evaluation_id: UUID) -> DataLicense:
    """Effective data-license row for a single evaluation — scalar sugar over the batch resolver.

    For single-item endpoints that hold one conversation/group (not the parent evaluation row),
    so they avoid the `[evaluation_id]` list-wrap plus `[evaluation_id]` dict-index dance. Same
    cost as the batch call with one id.
    """
    return (await resolve_effective_licenses(session, [evaluation_id]))[evaluation_id]


async def soft_delete_evaluation(session: AsyncSession, evaluation: Evaluation, *, by_id: UUID) -> Evaluation:
    """Mark ``evaluation`` as soft-deleted by stamping `deleted_at`."""
    evaluation.soft_delete(by_id)
    session.add(evaluation)
    await session.flush()
    await session.refresh(evaluation)
    return evaluation


async def get_restorable_evaluation(
    session: AsyncSession,
    evaluation_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
    deleted_cutoff: datetime,
) -> Evaluation:
    """Fetch the tombstoned evaluation ``evaluation_id`` this caller may restore.

    The parent group must still be live — restoring an evaluation under a dead group
    would return a row hidden from everyone by `_scope_to_groups`. No visibility
    predicate beyond that: the route authorizes the write through
    `authorize_evaluation_mutation` first, exactly as the delete does. The deleter
    predicate mirrors `list_evaluations`': the restore reaches exactly the set the
    deleted listing offered, and `evaluation_groups:manage` lifts both.

    Raises:
        NotFoundError: If no restorable evaluation with that id exists inside the window.
    """
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = (
        deleted_select(Evaluation, deleted_cutoff, deleted_by=None if can_manage else caller_id)
        .where(col(Evaluation.id) == evaluation_id)
        .join(EvaluationGroup, col(Evaluation.evaluation_group_id) == col(EvaluationGroup.id))
        .where(col(EvaluationGroup.deleted_at).is_(None))
        .with_for_update(of=Evaluation)
        .execution_options(populate_existing=True)
    )
    evaluation = (await session.execute(statement)).scalar_one_or_none()
    if evaluation is None:
        raise NotFoundError(f"No restorable evaluation {evaluation_id} was deleted within the restore window.")
    return evaluation


async def restore_evaluation(session: AsyncSession, evaluation: Evaluation) -> Evaluation:
    """Clear ``evaluation``'s tombstone, bringing its whole subtree back with it.

    Nothing to repair and no conflict to check: the delete tombstones only this row.
    Its scenarios, tasks, assignments and conversations were never marked — they
    disappear from reads through the parent-visibility join
    (`conversations._scope`, `_scope_to_groups`) and reappear the moment the parent
    is live again. Rows deleted in their own right stay deleted, since each carries
    its own `deleted_at`.
    """
    await restore_row(session, evaluation, conflict_message="Evaluation cannot be restored.")
    await session.refresh(evaluation)
    return evaluation
