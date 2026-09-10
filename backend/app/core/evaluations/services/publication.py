"""Evaluation-group publication transitions — submit / publish / finish / approve / request-changes / reject.

Same shape as the evaluation approval service (`services/approval.py`): an
allowed-transition table is the guard, so a wrong source state is a 409, not a
silent no-op. `submit` moves `draft`/`changes_requested → pending_approval`;
`approve`/`request-changes`/`reject` are the moderation verdicts on a
`pending_approval` group (`→ approved` / `→ changes_requested` / `→ not_approved`).
`changes_requested` is the non-terminal verdict — the owner can `submit` again.

Authorization is split per action: `submit`/`approve`/`publish`/`finish` follow the
group PATCH rule via `assert_group_write_access` (break-glass `evaluation_groups:manage`,
or an in-group role granting `evaluation_groups:update` — with the 404-then-403 split
coming from the visibility-scoped fetch), so the owner may approve their own group
(a deliberate decision). Only `request-changes`/`reject` are moderation verdicts the route
gates on `evaluation_groups:manage` alone (scope already lifted, no ownership check) —
so an owner can't bounce or reject their own group.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.roles import Permission
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.services.evaluation_groups import assert_group_publishable
from app.core.evaluations.services.evaluation_groups import assert_group_submittable
from app.core.evaluations.services.evaluation_groups import assert_group_write_access
from app.core.evaluations.services.evaluation_groups import get_evaluation_group
from app.core.exceptions import ConflictError
from app.core.notifications.enums import NotificationObjectType
from app.core.notifications.services.notifications import create_notification

_PUBLICATION_TRANSITIONS: dict[PublicationStatus, set[PublicationStatus]] = {
    PublicationStatus.DRAFT: {PublicationStatus.PENDING_APPROVAL},
    PublicationStatus.CHANGES_REQUESTED: {PublicationStatus.PENDING_APPROVAL},
    PublicationStatus.PENDING_APPROVAL: {
        PublicationStatus.APPROVED,
        PublicationStatus.NOT_APPROVED,
        PublicationStatus.CHANGES_REQUESTED,
    },
    PublicationStatus.APPROVED: {PublicationStatus.PUBLISHED},
    PublicationStatus.PUBLISHED: {PublicationStatus.INACTIVE},
}


def _assert_transition(group: EvaluationGroup, target: PublicationStatus) -> None:
    if target not in _PUBLICATION_TRANSITIONS.get(group.status, set()):
        raise ConflictError(f"Cannot move evaluation group {group.id} from '{group.status}' to '{target}'.")


async def _save(session: AsyncSession, group: EvaluationGroup) -> EvaluationGroup:
    session.add(group)
    await session.flush()
    await session.refresh(group, attribute_names=["updated_at"])
    return group


async def _notify_owner(
    session: AsyncSession, group: EvaluationGroup, *, actor_id: UUID, name: str, description: str
) -> None:
    """Tell the group owner of a moderation verdict — never the actor themselves (self-verdict is not news)."""
    if actor_id == group.created_by_id:
        return
    await create_notification(
        session,
        user_id=group.created_by_id,
        name=name,
        description=description,
        object_type=NotificationObjectType.EVALUATION_GROUP,
        object_id=group.id,
    )


async def submit_evaluation_group(
    session: AsyncSession,
    group_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
) -> tuple[EvaluationGroup, PublicationStatus]:
    """Submit a `draft` (or `changes_requested`) group for approval — `→ pending_approval`.

    Object-role gated like the group PATCH, with the same 404/403 split: an
    invisible (private, no relationship) group reads as missing, a visible one the
    caller may not write is forbidden. The owner submits their own group; a manager
    (break-glass) may submit any.

    Raises:
        NotFoundError: If no live group matches ``group_id`` visible to the caller.
        ForbiddenError: If the caller lacks write access to the group.
        ConflictError: If the group is not `draft` or `changes_requested`.
        BadRequestError: If the group is incomplete (a draft saved with gaps must be
            finished before review — see `assert_group_submittable`).
    """
    group = await get_evaluation_group(session, group_id, caller_id=caller_id, can_manage=can_manage, for_update=True)
    await assert_group_write_access(
        session,
        group_id=group.id,
        caller_id=caller_id,
        can_manage=can_manage,
        permission=Permission.EVALUATION_GROUPS_UPDATE,
        missing_message=f"Evaluation group {group.id} not found.",
    )
    previous_status = group.status  # read off the locked row — surfaced so the handler needn't re-read
    _assert_transition(group, PublicationStatus.PENDING_APPROVAL)
    # A draft may have been saved incomplete; entering review re-imposes the full
    # completeness checks the draft path skipped.
    await assert_group_submittable(session, group)
    group.status = PublicationStatus.PENDING_APPROVAL
    return await _save(session, group), previous_status


async def publish_evaluation_group(
    session: AsyncSession,
    group_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
) -> tuple[EvaluationGroup, PublicationStatus]:
    """Publish an `approved` group, making the transition `approved → published`.

    Object-role gated like the group PATCH, with the same 404/403 split: an
    invisible (private, no relationship) group reads as missing, a visible one the
    caller may not write is forbidden. `assert_group_publishable` gates the content
    an approved group is expected to have gained by now: at least one evaluation,
    each with at least one scenario (re-checked because scenarios are soft-deletable,
    so an evaluation may have lost its last one since the group entered review).

    Raises:
        NotFoundError: If no live group matches ``group_id`` visible to the caller.
        ForbiddenError: If the caller lacks write access to the group.
        ConflictError: If the group is not `approved`.
        BadRequestError: If the group has no evaluations, or a live evaluation of it
            has no live scenario.
    """
    group = await get_evaluation_group(session, group_id, caller_id=caller_id, can_manage=can_manage, for_update=True)
    await assert_group_write_access(
        session,
        group_id=group.id,
        caller_id=caller_id,
        can_manage=can_manage,
        permission=Permission.EVALUATION_GROUPS_UPDATE,
        missing_message=f"Evaluation group {group.id} not found.",
    )
    previous_status = group.status
    _assert_transition(group, PublicationStatus.PUBLISHED)
    await assert_group_publishable(session, group)
    group.status = PublicationStatus.PUBLISHED
    return await _save(session, group), previous_status


async def finish_evaluation_group(
    session: AsyncSession,
    group_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
) -> tuple[EvaluationGroup, PublicationStatus]:
    """Finish a `published` group, making the transition `published → inactive`.

    A pure state move — `end_date` stays the scheduled value, it is not stamped
    with the actual close. Authorization matches `publish_evaluation_group`.

    Raises:
        NotFoundError: If no live group matches ``group_id`` visible to the caller.
        ForbiddenError: If the caller lacks write access to the group.
        ConflictError: If the group is not `published`.
    """
    group = await get_evaluation_group(session, group_id, caller_id=caller_id, can_manage=can_manage, for_update=True)
    await assert_group_write_access(
        session,
        group_id=group.id,
        caller_id=caller_id,
        can_manage=can_manage,
        permission=Permission.EVALUATION_GROUPS_UPDATE,
        missing_message=f"Evaluation group {group.id} not found.",
    )
    previous_status = group.status
    _assert_transition(group, PublicationStatus.INACTIVE)
    group.status = PublicationStatus.INACTIVE
    return await _save(session, group), previous_status


async def approve_evaluation_group(
    session: AsyncSession,
    group_id: UUID,
    *,
    caller_id: UUID,
    can_manage: bool,
) -> tuple[EvaluationGroup, PublicationStatus]:
    """Approve a `pending_approval` group (`pending_approval → approved`).

    Object-role gated like the group PATCH (same 404/403 split): the owner may
    approve their own group, and a manager (break-glass) may approve any — owner OR
    manage, not manage-only. Clears any stale `rejection_reason` so an approved
    group never carries it on the wire (mirrors `approve_evaluation` in
    `services/approval.py`).

    Raises:
        NotFoundError: If no live group matches ``group_id`` visible to the caller.
        ForbiddenError: If the caller lacks write access to the group.
        ConflictError: If the group is not `pending_approval`.
    """
    group = await get_evaluation_group(session, group_id, caller_id=caller_id, can_manage=can_manage, for_update=True)
    await assert_group_write_access(
        session,
        group_id=group.id,
        caller_id=caller_id,
        can_manage=can_manage,
        permission=Permission.EVALUATION_GROUPS_UPDATE,
        missing_message=f"Evaluation group {group.id} not found.",
    )
    previous_status = group.status
    _assert_transition(group, PublicationStatus.APPROVED)
    group.status = PublicationStatus.APPROVED
    group.rejection_reason = None
    saved = await _save(session, group)
    await _notify_owner(
        session,
        saved,
        actor_id=caller_id,
        name="Evaluation group approved",
        description=f"Your evaluation group '{saved.title}' was approved.",
    )
    return saved, previous_status


async def request_changes_for_evaluation_group(
    session: AsyncSession,
    group_id: UUID,
    *,
    caller_id: UUID,
) -> tuple[EvaluationGroup, PublicationStatus]:
    """Bounce a `pending_approval` group back for edits (`pending_approval → changes_requested`).

    The moderator's non-terminal verdict — the owner can `submit` again from
    `changes_requested`. Gated on `evaluation_groups:manage` like `reject`, with the
    visibility scope lifted. Clears any stale `rejection_reason`.

    Raises:
        NotFoundError: If no live group matches ``group_id``.
        ConflictError: If the group is not `pending_approval`.
    """
    group = await get_evaluation_group(session, group_id, caller_id=caller_id, can_manage=True, for_update=True)
    previous_status = group.status
    _assert_transition(group, PublicationStatus.CHANGES_REQUESTED)
    group.status = PublicationStatus.CHANGES_REQUESTED
    group.rejection_reason = None
    saved = await _save(session, group)
    await _notify_owner(
        session,
        saved,
        actor_id=caller_id,
        name="Evaluation group changes requested",
        description=f"Changes were requested on your evaluation group '{saved.title}'.",
    )
    return saved, previous_status


async def reject_evaluation_group(
    session: AsyncSession,
    group_id: UUID,
    *,
    caller_id: UUID,
    rejection_reason: str,
) -> tuple[EvaluationGroup, PublicationStatus]:
    """Reject a `pending_approval` group, recording why (`pending_approval → not_approved`).

    A moderation action: the route gates it on `evaluation_groups:manage`, so
    the fetch runs with the visibility scope lifted and no ownership check
    applies here.

    Args:
        session: Async DB session bound to the request.
        group_id: Group to reject.
        caller_id: Acting user; carried through to the fetch, though `manage`
            already lifts the visibility scope it would constrain.
        rejection_reason: Why it was rejected; non-empty is enforced at the edge.

    Raises:
        NotFoundError: If no live group matches ``group_id``.
        ConflictError: If the group is not `pending_approval`.
    """
    group = await get_evaluation_group(session, group_id, caller_id=caller_id, can_manage=True, for_update=True)
    previous_status = group.status
    _assert_transition(group, PublicationStatus.NOT_APPROVED)
    group.status = PublicationStatus.NOT_APPROVED
    group.rejection_reason = rejection_reason
    saved = await _save(session, group)
    await _notify_owner(
        session,
        saved,
        actor_id=caller_id,
        name="Evaluation group rejected",
        description=f"Your evaluation group '{saved.title}' was rejected: {rejection_reason}",
    )
    return saved, previous_status
