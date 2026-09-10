"""Evaluation-group services: list, get, create, update.

The access rule (`public` OR member for reads; in-group role or break-glass for
writes) lives in `app.core.evaluations.access` and `assert_group_write_access`
below; `list_evaluation_groups` and `get_evaluation_group`
each document how they apply `group_visible_to` and how the `evaluation_groups:manage`
elevation widens it.
"""

from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC
from datetime import date
from datetime import datetime
from typing import assert_never
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col

from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import ObjectAccessContext
from app.core.auth.object_roles.service import effective_object_permissions
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.object_roles.service import held_roles
from app.core.auth.object_roles.service import resolve_object_access
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.schemas import SessionUser
from app.core.auth.services.roles import get_role_by_name
from app.core.evaluations.access import caller_live_organization_select
from app.core.evaluations.access import group_visible_to
from app.core.evaluations.access import resolve_metrics_scope
from app.core.evaluations.enums import GROUP_STATUSES_ACCEPTING_EVALUATIONS
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import MetricsAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.filters import EvaluationGroupFilters
from app.core.evaluations.filters import EvaluationGroupOrderBy
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import EvaluationGroupAiModel
from app.core.evaluations.models import EvaluationTagKey
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.evaluations.schemas import EvaluationGroupUpdateChanges
from app.core.evaluations.schemas import dates_out_of_order
from app.core.evaluations.services.group_models import sync_group_models
from app.core.evaluations.services.loaders import evaluation_models_loader
from app.core.exceptions import BadRequestError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.licenses.catalog import NO_LICENSE_SPDX_ID
from app.core.licenses.catalog import curated_license_id
from app.core.licenses.models import defer_license_text
from app.core.licenses.service import assert_curated_license_synced
from app.core.licenses.service import validate_license_ref
from app.core.ordering import apply_order_by
from app.core.organizations.models import Organization
from app.core.pagination import paginate
from app.core.soft_delete import with_live


@dataclass(frozen=True, slots=True)
class GroupContext:
    """A group plus the caller's resolved object-scope access to it — the gate's payload.

    `caller_org_id` is the caller's *live* organization id (None when orgless or
    the org is soft-deleted), carried so the in-Python visibility gate can apply
    the `organization` access arm without re-querying.
    """

    group: EvaluationGroup
    access: ObjectAccessContext
    caller_org_id: UUID | None


def _apply_evaluation_group_filters(
    statement: Select[tuple[EvaluationGroup]],
    filters: EvaluationGroupFilters,
) -> Select[tuple[EvaluationGroup]]:
    """Append a WHERE clause for every supplied filter, leave omitted ones alone."""
    if filters.status is not None:
        statement = statement.where(col(EvaluationGroup.status) == filters.status)
    if filters.accepts_evaluations is not None:
        accepting = col(EvaluationGroup.status).in_(GROUP_STATUSES_ACCEPTING_EVALUATIONS)
        statement = statement.where(accepting if filters.accepts_evaluations else ~accepting)
    if filters.access_level is not None:
        statement = statement.where(col(EvaluationGroup.access_level) == filters.access_level)
    if filters.search is not None:
        pattern = f"%{filters.search}%"
        statement = statement.where(
            or_(
                col(EvaluationGroup.title).ilike(pattern, escape="\\"),
                col(EvaluationGroup.description).ilike(pattern, escape="\\"),
            ),
        )
    return statement


async def list_evaluation_groups(
    session: AsyncSession,
    *,
    caller_id: UUID,
    show_all: bool = False,
    filters: EvaluationGroupFilters,
    order_by: EvaluationGroupOrderBy,
    limit: int,
    offset: int,
) -> tuple[list[EvaluationGroup], int]:
    """Return one page of live groups matching `filters`.

    By default the result is scoped to what `caller_id` may see (`public` or
    member), applied *before* the user filters so a filter can never widen it.
    `show_all` lifts that scope to every group; the route only sets it for a
    caller holding `evaluation_groups:manage`, so the permission gate lives there,
    not here.
    """
    statement = EvaluationGroup.live_select()
    if not show_all:
        statement = statement.where(group_visible_to(caller_id))
    statement = _apply_evaluation_group_filters(statement, filters)
    statement = apply_order_by(statement, EvaluationGroup, order_by)
    # `EvaluationGroup.data_license` is `lazy="selectin"`, so the licence rows arrive whether or not
    # they are asked for — and a page of them would carry a legal text per distinct licence, while the
    # response projects `DataLicenseSummary`, which has no text field. Naming the loader here is what
    # lets the column stay behind.
    statement = statement.options(
        selectinload(EvaluationGroup.data_license).options(  # ty: ignore[invalid-argument-type]
            defer_license_text()
        )
    )
    return await paginate(session, statement, limit=limit, offset=offset)


async def _validate_group_org(
    session: AsyncSession,
    *,
    access_level: EvaluationGroupAccessLevel,
    organization_id: UUID | None,
    lock: bool = True,
) -> None:
    """Enforce the `organization` access invariant: a live org must be set.

    The `organization` access level scopes a group's read-visibility to its owning
    org (`access.group_visible_to`), so it requires `organization_id`; and any set
    `organization_id` must reference a live (non-soft-deleted) org, so a group
    never carries a dangling or tombstoned reference. This guards the stored data
    at write time; the visibility rule re-checks liveness at read time (fail-closed).

    Args:
        session: the active session.
        access_level: the group's access level, decided by the caller's payload.
        organization_id: the org the group would carry, if any.
        lock: take the `FOR SHARE` row lock described below. On by default — create
            and update may attach a *new* org reference, so they have to serialize
            against a concurrent delete. `collect_submit_blockers` passes `False`, for
            the detail GET and the submit gate alike: the group already references the
            org, so `soft_delete_organization` 409s on it whatever the group's status,
            and locking would make a plain detail GET block on a concurrent delete.

    Raises:
        BadRequestError: `access_level` is `organization` without an
            `organization_id`, or `organization_id` is set but no live
            organization matches it.
    """
    if access_level == EvaluationGroupAccessLevel.ORGANIZATION and organization_id is None:
        raise BadRequestError("organization_id is required when access_level is 'organization'.")
    if organization_id is not None:
        # `FOR SHARE` so a concurrent `soft_delete_organization` (which locks the
        # org `FOR UPDATE` before counting referencing groups) serializes against
        # this write: either we hold the share lock and the delete blocks until our
        # group is committed (then it 409s), or the delete wins and our `live`
        # filter sees the tombstone and 400s. No window for a group to attach to a
        # just-deleted org.
        statement = select(col(Organization.id)).where(
            col(Organization.id) == organization_id,
            col(Organization.deleted_at).is_(None),
        )
        result = await session.execute(statement.with_for_update(read=True) if lock else statement)
        if result.scalar_one_or_none() is None:
            raise BadRequestError(f"organization_id {organization_id} does not reference a live organization.")


async def _assert_caller_may_assign_org(
    session: AsyncSession,
    *,
    caller_id: UUID,
    organization_id: UUID,
    can_manage: bool,
) -> None:
    """Forbid assigning a group to an organization the caller does not belong to.

    Parking a group in an org pushes it into that org's members' visibility, so a
    non-admin may target only their **own** live org — mirroring the admin-only
    user→org assignment and stopping cross-tenant planting. The
    `evaluation_groups:manage` break-glass lifts this (an admin may assign any live
    org). Caller-side only: that `organization_id` references a *live* org is
    validated separately (`_validate_group_org`), which callers run first so an
    unknown/dead org is a 400 before this 403.

    Raises:
        ForbiddenError: caller lacks break-glass and `organization_id` is not their
            own live organization.
    """
    if can_manage:
        return
    caller_org_id = await session.scalar(caller_live_organization_select(caller_id))
    if organization_id != caller_org_id:
        raise ForbiddenError("You may only assign a group to your own organization.")


async def collect_submit_blockers(session: AsyncSession, group: EvaluationGroup) -> list[str]:
    """List everything keeping a draft out of review — empty means submittable.

    A draft may be saved with gaps (only `title` is required at draft time); moving it
    to `pending_approval` re-imposes the same completeness a full create enforces, so an
    incomplete group can't reach review:

    - required fields present — `title`, `description`, `start_date`;
    - the same date rules as create — `start_date` not before today and, when set,
      `end_date` after `start_date`;
    - the organization invariant — `organization` access needs a live org, and any set
      `organization_id` must reference a live org;
    - at least one allowed model configured (the draft path allows an empty subset);
    - every live evaluation carries at least one live scenario — conversations target
      scenarios, so an evaluation without one would be unplayable once the group goes live.

    This is the completeness counterpart to `assert_group_write_access` (authority): the
    draft path skips these, the submit path restores them. `start_date >= today` is
    enforced here too (full create parity) — a group can't enter review opening in the
    past, even though a plain edit allows backdating.

    Collecting rather than raising on the first gap is what lets the group detail read
    advertise the full list before the owner clicks submit (`assert_group_submittable`
    wraps this for the transition itself).
    """
    blockers: list[str] = []
    # Human labels, not column names: these sentences are read in the console's readiness
    # panel. The create/update 400s keep naming the request field the caller actually sent.
    missing = [
        label
        for label, value in (
            ("title", group.title),
            ("description", group.description),
            ("start date", group.start_date),
        )
        if not value
    ]
    if missing:
        blockers.append(f"Fill in the required fields: {', '.join(missing)}.")
    if group.start_date is not None:
        if group.start_date < datetime.now(UTC).date():
            blockers.append("The start date must not be before today.")
        if dates_out_of_order(group.start_date, group.end_date):
            blockers.append("The end date must be after the start date.")
    try:
        # Reuse the write-path org guard rather than restating its rules and messages here,
        # minus its lock: the group already holds the reference, so a concurrent org delete
        # 409s on it at any status.
        await _validate_group_org(
            session, access_level=group.access_level, organization_id=group.organization_id, lock=False
        )
    except BadRequestError as exc:
        blockers.append(str(exc))
    has_model = await session.scalar(
        EvaluationGroupAiModel.live_select().where(col(EvaluationGroupAiModel.evaluation_group_id) == group.id).limit(1)
    )
    if has_model is None:
        blockers.append("Assign at least one allowed model.")
    if (scenario_blocker := await _missing_scenario_blocker(session, group.id)) is not None:
        blockers.append(scenario_blocker)
    return blockers


async def collect_publish_blockers(session: AsyncSession, group: EvaluationGroup) -> list[str]:
    """List everything keeping an approved group unpublished — empty means publishable.

    Two rules, both about content the group only gains *after* approval (evaluations may
    be added to an `approved`/`published` group only — see
    `GROUP_STATUSES_ACCEPTING_EVALUATIONS`), which is why they gate `publish` and not
    `submit`, where a fresh draft could never satisfy them:

    - at least one live evaluation — an empty engagement has nothing to play;
    - every live evaluation carries at least one live scenario. Re-checked here on top of
      the submit gate because scenarios are soft-deletable: the set can shrink while the
      group sits in review.

    Field/date/org completeness is not re-checked — `submit` already imposed it and the
    approved group carries the same values.
    """
    blockers: list[str] = []
    has_evaluation = await session.scalar(
        Evaluation.live_select().where(col(Evaluation.evaluation_group_id) == group.id).limit(1)
    )
    if has_evaluation is None:
        blockers.append("Add at least one evaluation to the group.")
    if (scenario_blocker := await _missing_scenario_blocker(session, group.id)) is not None:
        blockers.append(scenario_blocker)
    return blockers


async def collect_publication_blockers(session: AsyncSession, group: EvaluationGroup) -> list[str]:
    """List the gaps blocking this group's *next* lifecycle step — empty means ready.

    The read-side counterpart of the transition gates, dispatched on the current status
    so the group detail advertises exactly what the next click would refuse: `draft` and
    `changes_requested` are heading for `submit` (full completeness), `approved` for
    `publish` (content only). Every other status has no owner-fixable gap ahead of it —
    `pending_approval` waits on a moderator, `published` on the owner calling it a day
    (`finish` has no completeness gate), the terminal states on nothing — so the list
    is empty there rather than describing a transition that isn't offered.

    Shares the collectors with the 400s, so the field and the refusal can never disagree.
    """
    if group.status in (PublicationStatus.DRAFT, PublicationStatus.CHANGES_REQUESTED):
        return await collect_submit_blockers(session, group)
    if group.status == PublicationStatus.APPROVED:
        return await collect_publish_blockers(session, group)
    return []


async def assert_group_submittable(session: AsyncSession, group: EvaluationGroup) -> None:
    """Refuse to submit a group with any completeness gap, naming every one of them.

    Raises:
        BadRequestError: listing every blocker from `collect_submit_blockers`.
    """
    if blockers := await collect_submit_blockers(session, group):
        raise BadRequestError(f"Cannot submit an incomplete group: {' '.join(blockers)}")


async def assert_group_publishable(session: AsyncSession, group: EvaluationGroup) -> None:
    """Refuse to publish a group with any content gap, naming every one of them.

    Raises:
        BadRequestError: listing every blocker from `collect_publish_blockers`.
    """
    if blockers := await collect_publish_blockers(session, group):
        raise BadRequestError(f"Cannot publish an incomplete group: {' '.join(blockers)}")


async def _missing_scenario_blocker(session: AsyncSession, group_id: UUID) -> str | None:
    """Name the group's live evaluations that still have no live scenario, if any.

    Conversations target a scenario, so an evaluation without one is unplayable. Only
    live evaluations count — a tombstoned one no longer needs a scenario.
    """
    titles = (
        await session.scalars(
            select(col(Evaluation.title))
            .where(
                col(Evaluation.evaluation_group_id) == group_id,
                col(Evaluation.deleted_at).is_(None),
                ~select(col(Scenario.id))
                .where(
                    col(Scenario.evaluation_id) == col(Evaluation.id),
                    col(Scenario.deleted_at).is_(None),
                )
                .exists(),
            )
            .order_by(col(Evaluation.title))
        )
    ).all()
    if not titles:
        return None
    # Titles only, no ids: this sentence is read verbatim in the console's readiness panel, which
    # sits right above the list of these evaluations. A repeated title means two of them share it
    # and both need a scenario.
    return f"Every evaluation needs at least one scenario; add one to: {', '.join(titles)}."


def default_license_for_access(access_level: EvaluationGroupAccessLevel) -> UUID | None:
    """The `data_license_id` a create derives when the payload omits the field.

    `invitation_only` derives the no-license sentinel — its evaluations and conversations then
    inherit "no licence" instead of the platform default. Every other access level derives `None`
    (inherit). Applied on a create, and on an access-level change that finds no stored licence to
    keep (see `update_evaluation_group`) — a stored one is never rewritten.

    Exhaustive over the enum on purpose: a new access level should not pick up "inherit the platform
    default" by falling through, since that is the licence-leaking direction.
    """
    match access_level:
        case EvaluationGroupAccessLevel.INVITATION_ONLY:
            return curated_license_id(NO_LICENSE_SPDX_ID)
        case EvaluationGroupAccessLevel.PUBLIC | EvaluationGroupAccessLevel.ORGANIZATION:
            return None
        case _:
            assert_never(access_level)


async def _resolve_new_group_license(
    session: AsyncSession,
    *,
    data_license_id: UUID | None,
    data_license_set: bool,
    access_level: EvaluationGroupAccessLevel,
) -> UUID | None:
    """The licence a create stores, and the guard that goes with it.

    Three inputs, three outcomes: an id is validated as a live licence (400 if not), an explicit
    `null` (`data_license_set`) inherits, and an absent field derives from `access_level`. A derived
    id is checked with `assert_curated_license_synced` instead, because the caller never sent it —
    its absence is a server fault, not a bad request. Shared by both creators so the rule has one
    home.
    """
    if data_license_id is None and not data_license_set:
        derived = default_license_for_access(access_level)
        if derived is not None:
            await assert_curated_license_synced(session, derived)
        return derived
    if data_license_id is not None:
        await validate_license_ref(session, data_license_id)
    return data_license_id


async def create_evaluation_group(  # noqa: PLR0913 — keyword-only args mirror the group's column set plus the break-glass flag; a Pydantic carrier would just shift the surface area
    session: AsyncSession,
    *,
    caller_id: UUID,
    title: str,
    description: str,
    access_level: EvaluationGroupAccessLevel,
    metrics_access_during: MetricsAccessLevel = MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    metrics_access_after: MetricsAccessLevel = MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    organization_id: UUID | None = None,
    can_manage: bool = False,
    start_date: date,
    end_date: date | None = None,
    data_license_id: UUID | None = None,
    data_license_set: bool = False,
    allowed_model_ids: Collection[UUID] = (),
    status: PublicationStatus = PublicationStatus.DRAFT,
) -> EvaluationGroup:
    """Create a new complete group owned by `caller_id`.

    `status` is set by the caller, not the payload — it defaults to `DRAFT` (used
    by seeding); the public create endpoint passes `PENDING_APPROVAL` so a
    completed group goes straight to review, while partial drafts use the
    dedicated draft path. Both date rules live here (→ 400) so they share one code
    and envelope with the update's merged-state check: `start_date` must not be
    before today (UTC; create-only — edit allows backdating) and, when set,
    `end_date` must be after `start_date`. `can_manage` (the
    `evaluation_groups:manage` break-glass) lifts the own-org assignment constraint
    so an admin may set any live org.

    Raises:
        BadRequestError: If `start_date` is before today, `end_date` is set but
            not after `start_date`, or the org invariant fails (`access_level` is
            `organization` without a live `organization_id`).
        ForbiddenError: If a non-manage caller sets `organization_id` to an org
            they do not belong to.
    """
    if start_date < datetime.now(UTC).date():
        raise BadRequestError("start_date must not be before today.")
    if dates_out_of_order(start_date, end_date):
        raise BadRequestError("end_date must be after start_date.")
    await _validate_group_org(session, access_level=access_level, organization_id=organization_id)
    if organization_id is not None:
        await _assert_caller_may_assign_org(
            session, caller_id=caller_id, organization_id=organization_id, can_manage=can_manage
        )
    data_license_id = await _resolve_new_group_license(
        session, data_license_id=data_license_id, data_license_set=data_license_set, access_level=access_level
    )
    group = await _persist_new_group(
        session,
        caller_id=caller_id,
        title=title,
        description=description,
        access_level=access_level,
        metrics_access_during=metrics_access_during,
        metrics_access_after=metrics_access_after,
        organization_id=organization_id,
        start_date=start_date,
        end_date=end_date,
        data_license_id=data_license_id,
        status=status,
    )
    await sync_group_models(session, group_id=group.id, model_ids=allowed_model_ids, by_id=caller_id)
    return group


async def _grant_owner(session: AsyncSession, group_id: UUID, caller_id: UUID) -> None:
    """Grant the creator the in-group `owner` object-role.

    The creator's in-group authority (incl. manage_members) is carried by this
    `owner` object-role, not the `created_by_id` column — so demoting them to a
    lesser role later actually strips that authority (the per-object override).
    Shared by create / draft-create / duplicate.
    """
    owner_role = await get_role_by_name(session, SystemRole.OWNER.value)
    await grant_roles(session, ObjectType.EVALUATION_GROUP, group_id, caller_id, [owner_role])


async def _persist_new_group(  # noqa: PLR0913 — keyword-only args mirror the group's column set; a Pydantic carrier would just shift the surface area
    session: AsyncSession,
    *,
    caller_id: UUID,
    title: str | None,
    description: str | None,
    access_level: EvaluationGroupAccessLevel,
    metrics_access_during: MetricsAccessLevel = MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    metrics_access_after: MetricsAccessLevel = MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    organization_id: UUID | None,
    start_date: date | None,
    end_date: date | None,
    data_license_id: UUID | None,
    status: PublicationStatus,
) -> EvaluationGroup:
    """Build, persist, and owner-grant a new group — the tail shared by create / draft / duplicate.

    The caller does its own validation first; this just stamps `created_by_id`, adds
    the row, flushes (so `created_at`/`updated_at` are populated), and grants the
    creator the in-group `owner` role. Keeping it in one place means a future
    create-time concern (a new defaulted column, an audit hook, Stage-2 org
    stamping) lands for drafted and duplicated groups too, not just created ones.
    """
    group = EvaluationGroup(
        title=title,
        description=description,
        access_level=access_level,
        metrics_access_during=metrics_access_during,
        metrics_access_after=metrics_access_after,
        organization_id=organization_id,
        start_date=start_date,
        end_date=end_date,
        data_license_id=data_license_id,
        created_by_id=caller_id,
        status=status,
    )
    session.add(group)
    await session.flush()
    await session.refresh(group, attribute_names=["created_at", "updated_at", "data_license"])
    await _grant_owner(session, group.id, caller_id)
    return group


async def _load_evaluation_children(
    session: AsyncSession, evaluation_ids: list[UUID]
) -> tuple[dict[UUID, list[Scenario]], dict[UUID, list[EvaluationTagKey]]]:
    """Load the live scenarios (with tasks) and allowed tag keys of many evaluations, grouped by parent.

    Two `IN` queries for the whole set rather than a pair per evaluation — the anti-N+1 shape the
    group deep-copy needs. Scenarios come back in `position` order; tag keys are unordered.
    """
    scenarios_by_eval: dict[UUID, list[Scenario]] = defaultdict(list)
    tag_keys_by_eval: dict[UUID, list[EvaluationTagKey]] = defaultdict(list)
    if not evaluation_ids:
        return scenarios_by_eval, tag_keys_by_eval
    scenarios = (
        (
            await session.execute(
                Scenario.live_select()
                .where(col(Scenario.evaluation_id).in_(evaluation_ids))
                .options(selectinload(Scenario.tasks), with_live(Task))  # ty: ignore[invalid-argument-type]
                .order_by(col(Scenario.position))
            )
        )
        .scalars()
        .all()
    )
    for scenario in scenarios:
        scenarios_by_eval[scenario.evaluation_id].append(scenario)
    tag_keys = (
        (
            await session.execute(
                EvaluationTagKey.live_select().where(col(EvaluationTagKey.evaluation_id).in_(evaluation_ids))
            )
        )
        .scalars()
        .all()
    )
    for tag_key in tag_keys:
        tag_keys_by_eval[tag_key.evaluation_id].append(tag_key)
    return scenarios_by_eval, tag_keys_by_eval


async def duplicate_evaluation_group(
    session: AsyncSession,
    *,
    source_group_id: UUID,
    caller_id: UUID,
    can_manage: bool,
    include_children: bool = False,
) -> EvaluationGroup:
    """Copy a group into a fresh `draft` owned by the caller.

    Always copies the group's own fields and its allowed-model subset (referencing
    the same shared `AiModel` rows; a since-soft-deleted model is skipped). When
    ``include_children`` is set, also deep-copies its child evaluations, their
    scenarios + tasks, and their model assignments (again the same shared `AiModel`
    rows — no secrets: the API key lives on the `AiModel`, untouched). Runtime data
    is never copied: conversations, messages, flags, reviews. The copy starts in
    `draft`; child evaluations reset to their default status (`new`).

    Duplicating needs write access on the source — the same owner-or-manage gate as
    a group edit (`assert_group_write_access` for `evaluation_groups:update`): an
    in-group role granting that permission (the creator holds `owner`), or the
    break-glass `evaluation_groups:manage`. A caller who can merely *see* the source
    cannot. A soft-deleted owning org is dropped (the copy goes org-less) rather than
    carried as a dangling reference the create/update paths would later reject.

    Raises:
        NotFoundError: If no live source group matches ``source_group_id`` visible to the caller.
        ForbiddenError: If the caller can see the source but lacks write access on it.
    """
    source = await get_evaluation_group(
        session,
        source_group_id,
        caller_id=caller_id,
        can_manage=can_manage,
        with_evaluations=include_children,
        with_allowed_models=True,
    )
    # Cloning needs write access (an in-group role granting update, or manage), not
    # merely visibility — reuse the same group write gate as an edit.
    await assert_group_write_access(
        session,
        group_id=source.id,
        caller_id=caller_id,
        can_manage=can_manage,
        permission=Permission.EVALUATION_GROUPS_UPDATE,
        missing_message=f"Evaluation group {source_group_id} not found.",
    )
    # Drop a since-soft-deleted owning org rather than carrying a dangling reference.
    organization_id = source.organization_id
    if organization_id is not None:
        live_org = await session.execute(
            select(col(Organization.id)).where(
                col(Organization.id) == organization_id, col(Organization.deleted_at).is_(None)
            )
        )
        if live_org.scalar_one_or_none() is None:
            organization_id = None

    # The source's licence travels verbatim — never re-derived from the copy's access level,
    # which would strip a licence the source's delivered exports already name.
    copy = await _persist_new_group(
        session,
        caller_id=caller_id,
        title=source.title,
        description=source.description,
        access_level=source.access_level,
        metrics_access_during=source.metrics_access_during,
        metrics_access_after=source.metrics_access_after,
        organization_id=organization_id,
        start_date=source.start_date,
        end_date=source.end_date,
        data_license_id=source.data_license_id,
        status=PublicationStatus.DRAFT,
    )
    # Copy the allowed-model subset (reference the same shared `AiModel` rows; skip
    # any since-soft-deleted model so the copy can't resurrect a dead reference).
    for allowed in source.allowed_models:
        if allowed.ai_model is not None and allowed.ai_model.deleted_at is not None:
            continue
        session.add(EvaluationGroupAiModel(evaluation_group_id=copy.id, model_id=allowed.model_id))
    await session.flush()
    if not include_children:
        return copy

    # Deep-copy children. Every PK is a client-side UUID (BaseModel.id
    # default_factory), so a new row's id is available before flush — build the
    # whole graph and let the single trailing flush insert it in FK order.
    scenarios_by_eval, tag_keys_by_eval = await _load_evaluation_children(
        session, [src_eval.id for src_eval in source.evaluations]
    )

    # Evaluations carry no `position` (unlike scenarios) — they're an unordered set,
    # read back in the deterministic `EVALUATION_DEFAULT_ORDER` (`created_at`, then `id`)
    # like everywhere else. The clones share this transaction's `created_at`, so they
    # come back `id`-ordered, not source-ordered; that's accepted (no order to preserve).
    for src_eval in source.evaluations:
        new_eval = Evaluation(
            title=src_eval.title,
            description=src_eval.description,
            mask_models_enabled=src_eval.mask_models_enabled,
            tags_enabled=src_eval.tags_enabled,
            tags_restricted=src_eval.tags_restricted,
            cover_image=src_eval.cover_image,
            data_license_id=src_eval.data_license_id,
            created_by_id=caller_id,
            evaluation_group_id=copy.id,
        )
        session.add(new_eval)
        for src_tag_key in tag_keys_by_eval[src_eval.id]:
            session.add(EvaluationTagKey(evaluation_id=new_eval.id, key=src_tag_key.key))
        # Model assignments reference the same shared AiModel row; skip any whose
        # model is soft-deleted so a copy can't resurrect a dead reference.
        for src_assignment in src_eval.models:
            if src_assignment.ai_model is not None and src_assignment.ai_model.deleted_at is not None:
                continue
            session.add(
                EvaluationAiModel(
                    evaluation_id=new_eval.id,
                    model_id=src_assignment.model_id,
                    model_display_mask=src_assignment.model_display_mask,
                    parameters=dict(src_assignment.parameters),
                )
            )
        # Preserve each scenario's `position` verbatim (don't renumber).
        for src_scenario in scenarios_by_eval[src_eval.id]:
            new_scenario = Scenario(
                name=src_scenario.name,
                description=src_scenario.description,
                position=src_scenario.position,
                required_reviews=src_scenario.required_reviews,
                evaluation_id=new_eval.id,
            )
            session.add(new_scenario)
            for src_task in src_scenario.tasks:
                session.add(Task(name=src_task.name, description=src_task.description, scenario_id=new_scenario.id))

    await session.flush()
    return copy


async def create_evaluation_group_draft(  # noqa: PLR0913 — keyword-only args mirror the group's column set plus the break-glass flag; a Pydantic carrier would just shift the surface area
    session: AsyncSession,
    *,
    caller_id: UUID,
    title: str,
    description: str | None = None,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.INVITATION_ONLY,
    metrics_access_during: MetricsAccessLevel = MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    metrics_access_after: MetricsAccessLevel = MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
    organization_id: UUID | None = None,
    can_manage: bool = False,
    start_date: date | None = None,
    end_date: date | None = None,
    data_license_id: UUID | None = None,
    data_license_set: bool = False,
    allowed_model_ids: Collection[UUID] = (),
) -> EvaluationGroup:
    """Save a partial draft owned by `caller_id`, always in `DRAFT`.

    Only `title` is required. The *completeness* checks the full create enforces
    (required fields, `start_date` not before today, the `organization` invariant)
    are intentionally skipped here and re-imposed at submit: `submit_evaluation_group`
    runs `assert_group_submittable`, so a draft saved with gaps can't reach review.
    Editing a draft (PATCH) deliberately allows partial state and backdating in
    between. The one completeness rule kept *here* is date order, and only when both
    dates are supplied (so an obviously-broken pair can't be stored even as a draft).

    Tenant isolation is **not** a completeness check, so it still applies: a set
    `organization_id` runs the same cross-tenant guard as create/update — a
    non-manage caller may only target their own live org (`_assert_caller_may_assign_org`),
    so a draft can't plant a group into another tenant's visibility. Org *liveness*
    is deferred to completion (a manager parking a dead/unknown org reads as
    fail-closed-invisible until PATCH re-validates it).

    Raises:
        BadRequestError: If both dates are set and `end_date` is not after `start_date`.
        ForbiddenError: If a non-manage caller sets `organization_id` to an org they
            do not belong to.
    """
    if start_date is not None and end_date is not None and dates_out_of_order(start_date, end_date):
        raise BadRequestError("end_date must be after start_date.")
    if organization_id is not None:
        await _assert_caller_may_assign_org(
            session, caller_id=caller_id, organization_id=organization_id, can_manage=can_manage
        )
    data_license_id = await _resolve_new_group_license(
        session, data_license_id=data_license_id, data_license_set=data_license_set, access_level=access_level
    )
    group = await _persist_new_group(
        session,
        caller_id=caller_id,
        title=title,
        description=description,
        access_level=access_level,
        metrics_access_during=metrics_access_during,
        metrics_access_after=metrics_access_after,
        organization_id=organization_id,
        start_date=start_date,
        end_date=end_date,
        data_license_id=data_license_id,
        status=PublicationStatus.DRAFT,
    )
    # A draft may carry a partial (even empty) subset; completeness is re-checked at submit.
    await sync_group_models(session, group_id=group.id, model_ids=allowed_model_ids, by_id=caller_id)
    return group


async def resolve_group_user_permissions(
    session: AsyncSession, caller: SessionUser, group: EvaluationGroup
) -> list[str]:
    """Resolve `caller`'s effective object permissions on the group, as sorted strings.

    Wraps `resolve_object_access` + `effective_object_permissions` (registry-derived)
    so the detail route can populate `user_permissions` without reaching into the
    object-role layer. `evaluation_groups:view_metrics` is additionally injected
    whenever the group's configured level would grant the caller *any* metrics read
    (`resolve_metrics_scope` returns a scope, full or personal) — the FE gates its
    metrics fetch on this single key, so it must appear iff the metrics GET would
    return 200 — which holds because this list is produced only by the group-detail
    read, itself gated on the same `evaluation_groups:read` floor the metrics gate
    re-checks (this function mirrors only `resolve_metrics_scope`, not that floor).
    The *breadth* (full vs personal) is not signalled here: it rides the
    response's own `scope` field, because `view_personal_metrics` is a held
    `red_teamer` permission that already surfaces in `effective_object_permissions`
    for any member (even an `owner_only` group), so it can't double as the gate key.
    Takes the loaded row (not an id) because the policy reads `status` and the
    `metrics_access_*` columns; the route has already loaded (and visibility-checked)
    the group. Mirrors what the metrics gate authorizes — never a substitute for it.
    """
    access = await resolve_object_access(session, caller, ObjectType.EVALUATION_GROUP, group.id)
    permissions = set(effective_object_permissions(access))
    if resolve_metrics_scope(group=group, access=access) is not None:
        permissions.add(Permission.EVALUATION_GROUPS_VIEW_METRICS.value)
    return sorted(permissions)


async def resolve_group_context(session: AsyncSession, caller: SessionUser, group_id: UUID) -> GroupContext:
    """Load a group and resolve `caller`'s object-scope access to it.

    Loads the group regardless of visibility (404 only when it genuinely doesn't
    exist); the route gate decides visibility from the returned context so the
    same payload drives both the 404-hide and the permission check.

    Raises:
        NotFoundError: If no live group matches ``group_id``.
    """
    # can_manage=True loads the row without the visibility filter; the gate
    # (`group_is_visible`) judges visibility from the resolved access instead.
    group = await get_evaluation_group(session, group_id, caller_id=caller.id, can_manage=True)
    access = await resolve_object_access(session, caller, ObjectType.EVALUATION_GROUP, group_id)
    # `caller_org_id` feeds only the `organization` arm of `group_is_visible`; every
    # other visibility path (break-glass / object-role / public) short-circuits
    # before reading it, so skip the extra round-trip on non-`organization` groups.
    caller_org_id = (
        await session.scalar(caller_live_organization_select(caller.id))
        if group.access_level == EvaluationGroupAccessLevel.ORGANIZATION
        else None
    )
    return GroupContext(group=group, access=access, caller_org_id=caller_org_id)


async def assert_group_write_access(
    session: AsyncSession,
    *,
    group_id: UUID,
    caller_id: UUID,
    can_manage: bool,
    permission: Permission,
    missing_message: str,
) -> None:
    """Gate a write to a group (or one of its evaluations) on the caller's object-scope authority.

    Object authority comes solely from `ObjectRoleAssignment` rows — there is no
    `created_by_id` fallback (the creator holds the `owner` role, granted on
    create):

    - **Break-glass** (`can_manage`, i.e. `evaluation_groups:manage` in the JWT) always passes.
    - **Member** (holds an in-group role): the role is authoritative — pass only
      if one of the held *active* roles' `Role.permissions` includes `permission`. A
      demoted member is therefore denied even if they created the group.
    - **Non-member**: no authority, regardless of any global permission.

    The 403/404 split mirrors visibility (break-glass already returned): a caller
    who can see the group but lacks the permission gets 403; one who can't (a
    private group they have no relationship to) gets 404.

    Raises:
        ForbiddenError: If the caller can see the group but lacks ``permission``.
        NotFoundError: If the caller can't see the (non-public) group at all.
    """
    if can_manage:
        return
    roles = await held_roles(session, ObjectType.EVALUATION_GROUP, group_id, caller_id)
    if roles:
        granted = {perm for role in roles if role.is_active for perm in role.permissions}
        if permission.value in granted:
            return
    # Denied. The 403/404 split mirrors read-visibility: a caller who can still
    # *see* the group (public non-draft, in its org, or a member) gets 403; one who
    # can't gets 404. Re-query the single visibility chokepoint rather than
    # re-deriving it here, so the org arm + the public-draft rule and any
    # future rule stay in one place.
    visible = await session.scalar(
        EvaluationGroup.live_select().where(col(EvaluationGroup.id) == group_id).where(group_visible_to(caller_id))
    )
    if visible is not None:
        raise ForbiddenError(f"Caller lacks the '{permission}' permission for this group.")
    raise NotFoundError(missing_message)


async def get_evaluation_group(
    session: AsyncSession,
    group_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    for_update: bool = False,
    with_evaluations: bool = False,
    with_allowed_models: bool = False,
) -> EvaluationGroup:
    """Fetch one live `EvaluationGroup` by id, scoped to what `caller_id` may see.

    Visibility is baked into the query (not a follow-up check) so a group the
    caller can't see is indistinguishable from a missing one — callers can't
    forget to apply it and leak a private group's existence.

    Args:
        session: Async DB session bound to the request.
        group_id: Primary key of the row to fetch.
        caller_id: Visibility scope; a group this caller may not see reads as missing.
        can_manage: Caller holds `evaluation_groups:manage`; lifts the visibility
            scope so a manager resolves any group regardless of owner/access.
        for_update: Append `FOR UPDATE` so the row lock is taken with the read —
            use when the caller will mutate this row later in the same transaction.
        with_evaluations: Eager-load the group's live child evaluations — and,
            per child, its live model assignments plus each assignment's
            `ai_model` — so the detail response can embed the full evaluations
            without an async lazy-load. Only the detail read needs this; child
            evaluations inherit the group's visibility, so loading them adds no
            separate access check.
        with_allowed_models: Eager-load the group's live allowed-model subset
            rows plus each row's `ai_model`, for the detail embed (surfaced only
            to `models:read` holders — the route decides) and the duplicate copy.

    Raises:
        NotFoundError: If no live row matches ``group_id`` visible to the caller.
    """
    statement = (
        EvaluationGroup.live_select()
        .where(col(EvaluationGroup.id) == group_id)
        .options(
            selectinload(EvaluationGroup.organization),  # ty: ignore[invalid-argument-type]
            with_live(Organization),
            selectinload(EvaluationGroup.data_license).options(  # ty: ignore[invalid-argument-type]
                defer_license_text()
            ),
        )
        # Force a fresh load of `data_license` even when the row is already identity-map-pinned
        # (e.g. created earlier in the same session), so the in-memory cascade projection has it.
        .execution_options(populate_existing=True)
    )
    if not can_manage:
        statement = statement.where(group_visible_to(caller_id))
    if with_evaluations:
        statement = statement.options(
            selectinload(EvaluationGroup.evaluations).options(  # ty: ignore[invalid-argument-type]
                evaluation_models_loader(),
                # `Evaluation.data_license` is `lazy="raise"`; the detail embed's in-memory cascade
                # reads it, so load it explicitly here (only on this path, not standalone reads).
                selectinload(Evaluation.data_license).options(  # ty: ignore[invalid-argument-type]
                    defer_license_text()
                ),
            ),
            with_live(Evaluation),
            with_live(EvaluationAiModel),
        )
    if with_allowed_models:
        statement = statement.options(
            selectinload(EvaluationGroup.allowed_models).selectinload(  # ty: ignore[invalid-argument-type]
                EvaluationGroupAiModel.ai_model  # ty: ignore[invalid-argument-type]
            ),
            with_live(EvaluationGroupAiModel),
        )
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    group = result.scalar_one_or_none()
    if group is None:
        raise NotFoundError(f"Evaluation group {group_id} not found.")
    return group


async def update_evaluation_group(
    session: AsyncSession,
    *,
    group: EvaluationGroup,
    caller_id: UUID,
    can_manage: bool = False,
    changes: EvaluationGroupUpdateChanges,
) -> EvaluationGroup:
    """Apply ``changes`` to ``group``, gated on an in-group role unless the caller can manage.

    Writes only fields in `changes.model_fields_set` — omitted fields stay,
    explicit `None` clears `end_date`. Re-validates the date order on the
    **merged** state, so a partial edit can't leave `end_date <= start_date`.
    An access-level change re-derives `data_license_id` when the group carries
    none and the payload omits the field (`invitation_only` then lands on the
    no-license sentinel); a stored licence is never rewritten.

    Args:
        session: Async DB session bound to the request.
        group: The row to mutate (typically fetched `for_update`).
        caller_id: Acting user; needs `evaluation_groups:update` on the group via
            an in-group role (e.g. `owner`), unless ``can_manage``.
        can_manage: Caller holds `evaluation_groups:manage` (break-glass); lifts
            the gate so a manager may edit a group it has no role on.
        changes: Validated partial-update contract.

    Raises:
        ForbiddenError: If the caller lacks `evaluation_groups:update` on the group,
            or assigns the group to an org they do not belong to (non-manage only).
        BadRequestError: If the merged start/end dates are out of order, or the
            merged org state is invalid (`organization` access without a live
            `organization_id`).
    """
    await assert_group_write_access(
        session,
        group_id=group.id,
        caller_id=caller_id,
        can_manage=can_manage,
        permission=Permission.EVALUATION_GROUPS_UPDATE,
        missing_message=f"Evaluation group {group.id} not found.",
    )
    # The own-org constraint applies only when the org actually *changes* — re-sending
    # the same value (clients PATCH the whole form) or editing other fields of a group
    # an admin parked in a foreign org must not trip it; only a genuine move does.
    original_org_id = group.organization_id
    original_access_level = group.access_level
    if "data_license_id" in changes.model_fields_set and changes.data_license_id is not None:
        await validate_license_ref(session, changes.data_license_id)
    # `allowed_model_ids` is not a column — route it through the subset replace-set
    # (liveness 400 / in-use 409), apart from the plain-column setattr loop.
    for field in changes.model_fields_set:
        if field == "allowed_model_ids":
            continue
        setattr(group, field, getattr(changes, field))
    # A group that inherits has no licence to keep, so an access-level change re-derives one; an
    # explicit id and an explicit `null` are both decisions and stay. Re-sending the same level is
    # not a change, so a full-form PATCH that only edits other fields never touches the licence.
    if (
        group.access_level != original_access_level
        and group.data_license_id is None
        and "data_license_id" not in changes.model_fields_set
    ):
        derived = default_license_for_access(group.access_level)
        if derived is not None:
            await assert_curated_license_synced(session, derived)
            group.data_license_id = derived
    if "allowed_model_ids" in changes.model_fields_set and changes.allowed_model_ids is not None:
        await sync_group_models(session, group_id=group.id, model_ids=changes.allowed_model_ids, by_id=caller_id)
    # `start_date` may be null on a partial draft mid-completion; only the
    # both-dates-set pair can be out of order.
    if group.start_date is not None and dates_out_of_order(group.start_date, group.end_date):
        raise BadRequestError("end_date must be after start_date.")
    # Validate the merged org state: `organization` access needs a live org, and
    # clearing the org (or switching to `organization` without one) is rejected.
    await _validate_group_org(session, access_level=group.access_level, organization_id=group.organization_id)
    if group.organization_id is not None and group.organization_id != original_org_id:
        await _assert_caller_may_assign_org(
            session, caller_id=caller_id, organization_id=group.organization_id, can_manage=can_manage
        )
    session.add(group)
    await session.flush()
    await session.refresh(group, attribute_names=["updated_at", "data_license"])
    return group
