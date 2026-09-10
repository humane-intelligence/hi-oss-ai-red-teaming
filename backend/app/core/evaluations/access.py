"""The evaluation-group read-visibility rule — `public` OR holds an object role.

One home for the visibility predicate so every read path agrees on who may see a
group. The SQL form (`group_visible_to`) filters list/get queries (groups, and
evaluations one level down); the in-Python form (`group_is_visible`) drives the
member-management gates and the 403/404 split. The metrics-access policy
(`resolve_metrics_scope`) lives here too — it layers *on top of*
visibility, never replaces it. Write authorization lives with the
object-role layer instead (`services.evaluation_groups.assert_group_write_access`),
since it needs a session. Pure policy here — no session, no I/O.

Object authority is carried entirely by `ObjectRoleAssignment` rows — there is no
`created_by_id` fallback. A group's creator sees and controls it because the
create path grants them the in-group `owner` role; `created_by_id` is attribution
only.
"""

from typing import Protocol
from uuid import UUID

from sqlalchemy import ColumnElement
from sqlalchemy import Select
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Mapped
from sqlmodel import col

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.models import ObjectRoleAssignment
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import ObjectAccessContext
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.conversations.models import Conversation
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import MetricsAccessLevel
from app.core.evaluations.enums import MetricsScope
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.organizations.models import Organization


def caller_live_organization_select(caller_id: UUID) -> Select[tuple[UUID]]:
    """Select the caller's organization id — yielded only when the user *and* org are live.

    Shared by the SQL visibility predicate (embedded as a scalar subquery) and the
    single-object gate (executed for the caller's live org id). Reads live DB
    state, not the frozen JWT `organization_id`, so an org reassignment or a
    soft-deleted org takes effect immediately — a tombstoned org reads as orgless,
    keeping the `organization` visibility arm fail-closed.
    """
    return (
        select(col(Organization.id))
        .select_from(User)
        .join(Organization, col(User.organization_id) == col(Organization.id))
        .where(
            col(User.id) == caller_id,
            col(User.deleted_at).is_(None),
            col(Organization.deleted_at).is_(None),
        )
    )


def group_visible_to(caller_id: UUID) -> ColumnElement[bool]:
    """SQL predicate for the read-visibility rule: `public` (and not a draft), the caller's org, or a member.

    Filter a `Select` on `EvaluationGroup` (directly or joined from a child) with
    this; pair it with the group's own `deleted_at IS NULL` liveness where the
    group isn't already the live-selected primary entity. Three disjoint arms:

    - **`public`** — visible platform-wide, regardless of any `organization_id` —
      **except a `draft`**: an incomplete work-in-progress stays owner/manager-only,
      so the `public` arm is gated on `status != draft`. Members (the
      owner) and break-glass managers still see their drafts via the membership arm
      / the `can_manage` lift the callers apply.
    - **`organization`** — visible only when the group's org equals the caller's
      *live* org (`caller_live_organization_select`). Fail-closed: an orgless
      caller, a soft-deleted caller-org, or a tombstoned group-org all yield a
      NULL subquery and so never match.
    - **member** — any live object-role assignment the caller holds on the group
      (any access level), so cross-org sharing rides the object-role layer and a
      removed member loses visibility.

    The membership arm checks assignment liveness only, not the underlying
    `Role`'s — unlike the write-side `held_roles`, which also requires a live
    `Role`. A member whose *catalog* role was soft-deleted (a rare admin action)
    thus still sees the group in listings but is denied on member operations.
    Accepted: object roles, not visibility, are the authority, so the mismatch
    degrades safely toward less power, and a tombstoned catalog role is an anomaly
    to fix at the source.
    """
    member_group_ids = select(col(ObjectRoleAssignment.object_id)).where(
        col(ObjectRoleAssignment.object_type) == ObjectType.EVALUATION_GROUP,
        col(ObjectRoleAssignment.user_id) == caller_id,
        col(ObjectRoleAssignment.deleted_at).is_(None),
    )
    caller_org = caller_live_organization_select(caller_id).scalar_subquery()
    return or_(
        and_(
            col(EvaluationGroup.access_level) == EvaluationGroupAccessLevel.PUBLIC,
            col(EvaluationGroup.status) != PublicationStatus.DRAFT,
        ),
        col(EvaluationGroup.id).in_(member_group_ids),
        and_(
            col(EvaluationGroup.access_level) == EvaluationGroupAccessLevel.ORGANIZATION,
            col(EvaluationGroup.organization_id) == caller_org,
        ),
    )


def groups_granting(permission: Permission, caller_id: UUID) -> Select[tuple[UUID]]:
    """Ids of the groups where ``caller_id`` holds an in-group role granting ``permission``.

    The object-scope counterpart of a JWT permission check, in SQL form, for read
    queries that widen per group rather than per request. Liveness is required on both
    sides — a soft-deleted assignment and a soft-deleted or deactivated `Role` both
    grant nothing — matching `assert_group_write_access`, which reads the same authority
    from held roles rather than from `created_by_id`.
    """
    return (
        select(col(ObjectRoleAssignment.object_id))
        .join(Role, col(ObjectRoleAssignment.role_id) == col(Role.id))
        .where(
            col(ObjectRoleAssignment.object_type) == ObjectType.EVALUATION_GROUP,
            col(ObjectRoleAssignment.user_id) == caller_id,
            col(ObjectRoleAssignment.deleted_at).is_(None),
            col(Role.deleted_at).is_(None),
            col(Role.is_active).is_(True),
            col(Role.permissions).contains([permission.value]),
        )
    )


def join_visible_evaluation_group[T](
    statement: Select[tuple[T]],
    evaluation_id: Mapped[UUID],
    *,
    caller_id: UUID,
    can_manage: bool,
) -> Select[tuple[T]]:
    """Join a child's evaluation FK to its live `Evaluation` + live `EvaluationGroup`, scoped to visibility.

    The shared spine of every "resolve a child of an evaluation under the group's
    read-visibility rule" query — `scenarios`, `tasks` (one level deeper, via its
    scenario), and `conversations`. Joins ``evaluation_id`` to a live `Evaluation`,
    then to its live `EvaluationGroup`, and — unless ``can_manage`` — constrains to
    `group_visible_to(caller_id)`. Parent liveness is enforced regardless of
    ``can_manage``; the elevation lifts only the `public`-or-member predicate.
    Callers layer their own extra predicates (e.g. the conversation owner check) on
    the returned statement.

    Args:
        statement: A `Select` over the child entity to constrain.
        evaluation_id: The child's evaluation FK column (e.g. `col(Scenario.evaluation_id)`).
        caller_id: The requesting user, for the membership predicate.
        can_manage: When true, lift the `public`-or-member predicate (break-glass).

    Returns:
        The statement with the joins and (unless ``can_manage``) visibility predicate applied.
    """
    statement = (
        statement.join(Evaluation, evaluation_id == col(Evaluation.id))
        .where(col(Evaluation.deleted_at).is_(None))
        .join(EvaluationGroup, col(Evaluation.evaluation_group_id) == col(EvaluationGroup.id))
        .where(col(EvaluationGroup.deleted_at).is_(None))
    )
    if not can_manage:
        statement = statement.where(group_visible_to(caller_id))
    return statement


class ConversationScopedChild(Protocol):
    """A row denormalising the ancestry `join_conversation_scoped` needs."""

    evaluation_id: UUID
    conversation_id: UUID
    created_by_id: UUID


def join_conversation_scoped[T: ConversationScopedChild](
    statement: Select[tuple[T]],
    model: type[T],
    *,
    caller_id: UUID,
    can_manage: bool,
    author_scoped: bool = True,
) -> Select[tuple[T]]:
    """Constrain ``statement`` to rows on a live, group-visible conversation.

    The caller's own rows, unless ``author_scoped`` is off. The read scope shared by every
    child that denormalises `conversation_id` + `evaluation_id` + `created_by_id` (message
    flags, task completions, notes, annotations): the visibility spine above, plus a join
    through the live parent
    `Conversation`, plus the author predicate. ``can_manage`` lifts the
    `public`-or-member predicate and the author predicate; ancestor **liveness** is
    enforced regardless, so soft-deleting any ancestor — including the conversation
    cascades wired for model-unassign / delete — hides the row from everyone, with
    no dedicated cascade hook.

    One home on purpose: this is authorization, so a predicate added to one entity's
    reads and not the others' would be a silent hole rather than an inconsistency.
    ``author_scoped=False`` is the one sanctioned divergence, and it lives here for the
    same reason: annotation reads are deliberately shared (any reader sees every
    annotation in a visible group — a tag exists to be aggregated), so they drop the
    author predicate while keeping the visibility spine and ancestor liveness.

    Args:
        statement: A `Select` over the child entity to constrain.
        model: The child model class, read for its three denormalised columns.
        caller_id: The requesting user — the author predicate and the membership one.
        can_manage: When true, lift the visibility and author predicates (break-glass).
        author_scoped: When false, skip the author predicate; visibility still applies.
    """
    statement = join_visible_evaluation_group(
        statement, col(model.evaluation_id), caller_id=caller_id, can_manage=can_manage
    )
    statement = statement.join(Conversation, col(model.conversation_id) == col(Conversation.id)).where(
        col(Conversation.deleted_at).is_(None)
    )
    if author_scoped and not can_manage:
        statement = statement.where(col(model.created_by_id) == caller_id)
    return statement


def group_is_visible(
    *,
    group: EvaluationGroup,
    caller: SessionUser,
    is_member: bool,
    caller_org_id: UUID | None,
) -> bool:
    """In-Python mirror of `group_visible_to`, plus the break-glass elevation.

    The single-object counterpart used by the member-management gates: a group is
    visible when the caller carries the `evaluation_groups:manage` break-glass
    permission, holds an object role on it, it is `public` **and not a draft** (a
    public draft is owner/manager-only), or it is an `organization` group
    whose org matches the caller's live org. Groups failing this read as 404 so the
    gate never leaks a private (or draft) group's existence.

    ``caller_org_id`` is the caller's *live* org id (resolved upstream via
    `caller_live_organization_select`, not the frozen JWT), and `group.organization`
    must be eager-loaded; `Organization.live` re-guards a soft-deleted org so the
    `organization` arm stays fail-closed, matching the SQL predicate.
    """
    if Permission.EVALUATION_GROUPS_MANAGE.value in caller.permissions:
        return True
    if is_member:
        return True
    if group.access_level == EvaluationGroupAccessLevel.PUBLIC:
        # A public *draft* is owner/manager-only; those callers already
        # returned True above, so a non-member non-manager sees it only once non-draft.
        return group.status != PublicationStatus.DRAFT
    if group.access_level == EvaluationGroupAccessLevel.ORGANIZATION:
        live_org = Organization.live(group.organization)
        return live_org is not None and caller_org_id is not None and live_org.id == caller_org_id
    return False


def resolve_metrics_scope(*, group: EvaluationGroup, access: ObjectAccessContext) -> MetricsScope | None:
    """The breadth of metrics the caller may read under the group's configured level, or `None` if denied.

    Assumes the caller already passed the visibility gate — an invisible group
    must read as 404 *before* this runs, which is also what makes
    `inherit_group_access` mean "the group's own audience" here: whoever reached
    this point can see the group per its `access_level` (never anonymous). The
    `view_metrics` check keeps the in-group `owner` (the only role granting it)
    and the break-glass admin on the `FULL` event-wide dashboards at every level.
    The configured level then decides everyone else:

    - `inherit_group_access` / `all_members` grant `FULL` (the latter only to a
      member) — the whole-event aggregate.
    - `members_personal_metrics` grants `PERSONAL` to a member holding
      `view_personal_metrics` (the `red_teamer` role) — the same dashboards
      filtered to *their own* contributions. A *permission* check, not a role-name
      one, so a future custom role opts in by carrying it; a multi-role member
      qualifies as long as one of their roles grants it.
    - `owner_only` grants nobody beyond the `view_metrics` holders above.

    `metrics_access_after` applies to a finished group (`inactive`),
    `metrics_access_during` to every other status — pre-published states count
    as "during" (visibility already confines them to members).
    """
    if access.has(Permission.EVALUATION_GROUPS_VIEW_METRICS):
        return MetricsScope.FULL
    level = group.metrics_access_after if group.status == PublicationStatus.INACTIVE else group.metrics_access_during
    if level == MetricsAccessLevel.INHERIT_GROUP_ACCESS:
        return MetricsScope.FULL
    if level == MetricsAccessLevel.ALL_MEMBERS:
        return MetricsScope.FULL if access.is_member else None
    if level == MetricsAccessLevel.MEMBERS_PERSONAL_METRICS:
        if access.is_member and access.has(Permission.EVALUATION_GROUPS_VIEW_PERSONAL_METRICS):
            return MetricsScope.PERSONAL
        return None
    return None


def caller_can_manage_groups(caller: SessionUser) -> bool:
    """True when the caller carries the JWT `evaluation_groups:manage` break-glass permission.

    The cross-cutting "is this an evaluation-group manager?" check the child-resource
    routers (`scenarios`, `tasks`, `conversations`) feed as ``can_manage`` into their
    services to lift the visibility/write scope.
    """
    return Permission.EVALUATION_GROUPS_MANAGE.value in caller.permissions
