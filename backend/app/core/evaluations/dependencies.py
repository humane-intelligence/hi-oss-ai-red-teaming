"""Evaluation-group-specific FastAPI dependency aliases.

Cross-cutting aliases (DB session, pagination, current user) live in
`app/core/dependencies.py`; resource-specific ones live here, per the API skill.
Per-group authorization gates resolve object-scope permissions from the generic
object-role layer (`app/core/auth/object_roles/`) and mirror `require_permission`.
"""

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from sqlmodel import col

from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import assert_object_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import CurrentUserDep
from app.core.dependencies import DbSession
from app.core.evaluations.access import group_is_visible
from app.core.evaluations.access import resolve_metrics_scope
from app.core.evaluations.enums import MetricsScope
from app.core.evaluations.filters import EvaluationAiModelFilters
from app.core.evaluations.filters import EvaluationFilters
from app.core.evaluations.filters import EvaluationGroupFilters
from app.core.evaluations.filters import ScenarioFilters
from app.core.evaluations.models import Evaluation
from app.core.evaluations.services.evaluation_groups import GroupContext
from app.core.evaluations.services.evaluation_groups import resolve_group_context
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError

EvaluationGroupFiltersDep = Annotated[EvaluationGroupFilters, Depends()]
EvaluationFiltersDep = Annotated[EvaluationFilters, Depends()]
EvaluationAiModelFiltersDep = Annotated[EvaluationAiModelFilters, Depends()]


def _not_visible(group_id: UUID) -> NotFoundError:
    # Hide groups the caller has no relationship to behind a 404 rather than
    # leaking their existence with a 403.
    return NotFoundError(f"Evaluation group {group_id} not found.")


def _visible(context: GroupContext, caller: CurrentUserDep) -> bool:
    return group_is_visible(
        group=context.group,
        caller=caller,
        is_member=context.access.is_member,
        caller_org_id=context.caller_org_id,
    )


def _require_global_read(caller: CurrentUserDep) -> None:
    # The `evaluation_groups:read` floor every group read enforces before visibility,
    # so a read sub-resource (members, metrics) can't be reached past a group the
    # caller couldn't open. A no-op in production (every system role holds `read`);
    # it keeps the surface honest for a deliberately narrowed role.
    if Permission.EVALUATION_GROUPS_READ not in caller.permissions:
        raise ForbiddenError(f"Caller lacks the '{Permission.EVALUATION_GROUPS_READ}' permission.")


async def require_group_read(group_id: UUID, caller: CurrentUserDep, db: DbSession) -> GroupContext:
    """Resolve the group context for a read gated like `GET /evaluation-groups/{id}`.

    Requires the global `evaluation_groups:read` permission *and* group
    visibility (public, owned, an object role held, or break-glass admin), so
    read sub-resources (e.g. listing members) present the same surface as the
    group detail endpoint. The permission check precedes visibility: a caller
    without the permission gets 403, an invisible group reads as 404.
    """
    _require_global_read(caller)
    context = await resolve_group_context(db, caller, group_id)
    if not _visible(context, caller):
        raise _not_visible(group_id)
    return context


def require_group_permission(permission: Permission) -> Callable[..., Awaitable[GroupContext]]:
    """Build a route dependency gating on an object-scope ``permission`` for the group.

    Mirrors `require_permission` but resolves the permission per group via the
    object-role layer. The caller passes only with **object-scope authority**:
    break-glass admins, or an in-group role granting ``permission`` (the creator
    holds `owner` by default). A member with a lesser role does not pass even if
    they carry the matching permission globally (the per-object override), and a
    non-member's global permission gives no say. Groups the caller can't see read
    as 404; visible-but-insufficient reads as 403.

    Args:
        permission: Object-scope permission key the caller must hold here.

    Returns:
        A dependency yielding the resolved `GroupContext`.
    """

    async def _check(group_id: UUID, caller: CurrentUserDep, db: DbSession) -> GroupContext:
        context = await resolve_group_context(db, caller, group_id)
        if not _visible(context, caller):
            raise _not_visible(group_id)
        # Object authority is the held roles' permissions (or break-glass) — never
        # the `created_by_id` column.
        if context.access.has(permission):
            return context
        raise ForbiddenError(f"Caller lacks the '{permission}' permission for this group.")

    return _check


# Resolves the group a request names, from whatever FastAPI injected into the resolver.
# Deferred rather than a plain `UUID | None` so the lookup is skipped for a caller who
# already holds the permission globally — the common case, and the short-circuit each
# gate had before they shared this factory.
GroupLookup = Callable[[], Awaitable[UUID | None]]


def require_object_or_global(
    permission: Permission, resolve_group: Callable[..., Awaitable[GroupLookup]]
) -> Callable[..., Awaitable[SessionUser]]:
    """Build a route dependency gating on ``permission`` held globally **or** in the named group.

    The counterpart of `require_group_permission` above for routes whose authority may come
    from *either* source. `require_permission` reads the JWT alone, and the JWT carries only
    global roles, so an object-role assignment never satisfied it: a caller added to a group
    as `red_teamer` could not start a conversation there despite that role granting the whole
    conversation CRUD set. The global permission still passes on its own — a red-teamer's
    reach into any evaluation they can *see* is the existing contract — which is exactly what
    separates this from `require_group_permission`'s object-authority-only rule.

    ``resolve_group`` is a dependency in its own right, so each route family declares the
    inputs it resolves the group from (a path id, a filter set) while the ordering that
    matters — global arm first, then object authority, refuse on an unresolved object — lives
    here once.

    Args:
        permission: Permission key the caller must hold globally or in-group.
        resolve_group: Dependency yielding a deferred lookup of the target group id.

    Returns:
        A dependency yielding the authenticated `SessionUser`.
    """

    async def _check(
        caller: CurrentUserDep, db: DbSession, lookup: Annotated[GroupLookup, Depends(resolve_group)]
    ) -> SessionUser:
        if permission in caller.permissions:
            return caller
        await assert_object_permission(db, caller, ObjectType.EVALUATION_GROUP, await lookup(), permission)
        return caller

    return _check


GroupReadDep = Annotated[GroupContext, Depends(require_group_read)]
ManageMembersDep = Annotated[
    GroupContext, Depends(require_group_permission(Permission.EVALUATION_GROUPS_MANAGE_MEMBERS))
]


@dataclass(frozen=True, slots=True)
class GroupMetricsView:
    """A resolved group plus the caller's personal-scope marker, for the dashboard route.

    ``viewer_id`` is the caller when the granted scope is `PERSONAL` (the service
    filters every breakdown to their own contributions) and ``None`` when `FULL`
    (the event-wide aggregate) — so the single field encodes the scope: the
    service reads `PERSONAL` iff it is set.

    ``full_model_access`` is the caller's `view_metrics` holding, carried separately
    from the scope because it answers a different question: the group dashboard's
    per-model spend roll-up correlates one model's cost across the group's
    evaluations, and per-evaluation masking exists to withhold exactly that. The
    evaluation-level view can pre-resolve masking into one boolean because it has a
    single evaluation to consult; the group spans several, so the service ANDs this
    with each evaluation's `mask_models_enabled`.
    """

    context: GroupContext
    viewer_id: UUID | None
    full_model_access: bool


@dataclass(frozen=True, slots=True)
class EvaluationMetricsView:
    """A resolved evaluation plus the caller's personal-scope marker and masking decision.

    ``viewer_id`` encodes the scope as in `GroupMetricsView`. ``mask_model_names``
    is true when the evaluation masks models *and* the caller is not a full-access
    `view_metrics` holder — so a lesser member (personal or `all_members`) sees
    each model's display mask, never its real identity, while the owner/admin
    always sees the real names. ``context`` is the parent group's resolved access context, carried
    like `GroupMetricsView` does — the dashboard's timeline reads the group's dates for its axis.
    """

    evaluation: Evaluation
    context: GroupContext
    viewer_id: UUID | None
    mask_model_names: bool


async def require_group_metrics_access(group_id: UUID, caller: CurrentUserDep, db: DbSession) -> GroupMetricsView:
    """Resolve the group context for its metrics dashboard, gated by the configured access level.

    Metrics are a reporting view whose audience the group itself configures:
    `metrics_access_during` / `metrics_access_after` decide who reads them beyond
    the object-scope `evaluation_groups:view_metrics` baseline (the in-group
    `owner`, or the `evaluation_groups:manage` break-glass — those always see the
    full aggregate). Requires the global `evaluation_groups:read` permission
    first — the same floor the group detail read enforces — so the metrics surface
    can't be reached by a caller who couldn't open the group itself. Then the
    visibility check (invisible group → 404) precedes the policy
    (`resolve_metrics_scope`), so visible-but-insufficient reads as 403. The
    returned view carries the granted `scope` (and, when `PERSONAL`, the caller as
    `viewer_id`) so the route filters the breakdown accordingly.
    """
    _require_global_read(caller)
    context = await resolve_group_context(db, caller, group_id)
    if not _visible(context, caller):
        raise _not_visible(group_id)
    scope = resolve_metrics_scope(group=context.group, access=context.access)
    if scope is None:
        raise ForbiddenError("Caller lacks metrics access for this group.")
    viewer_id = caller.id if scope == MetricsScope.PERSONAL else None
    return GroupMetricsView(
        context=context,
        viewer_id=viewer_id,
        full_model_access=context.access.has(Permission.EVALUATION_GROUPS_VIEW_METRICS),
    )


GroupMetricsDep = Annotated[GroupMetricsView, Depends(require_group_metrics_access)]


async def require_evaluation_metrics(
    evaluation_id: UUID, caller: CurrentUserDep, db: DbSession
) -> EvaluationMetricsView:
    """Resolve a single evaluation for its metrics dashboard, gated like the group dashboard.

    An evaluation's metrics are the parent group's reporting view scoped to one evaluation, so the
    gate mirrors `GroupMetricsDep`: the caller must hold the global `evaluation_groups:read`
    permission, see the parent group, and pass its configured metrics-access level
    (`resolve_metrics_scope` — the in-group `owner` and the `evaluation_groups:manage` break-glass
    always get the full aggregate). Lacking the permission is 403; a missing evaluation — or one
    whose group isn't visible — reads as 404; visible-but-insufficient reads as 403. The returned
    view carries the granted `scope`, the personal `viewer_id`, and whether model names must be
    masked for this caller.
    """
    _require_global_read(caller)
    evaluation = (
        await db.execute(Evaluation.live_select().where(col(Evaluation.id) == evaluation_id))
    ).scalar_one_or_none()
    if evaluation is None:
        raise NotFoundError(f"Evaluation {evaluation_id} not found.")
    context = await resolve_group_context(db, caller, evaluation.evaluation_group_id)
    if not _visible(context, caller):
        raise NotFoundError(f"Evaluation {evaluation_id} not found.")
    scope = resolve_metrics_scope(group=context.group, access=context.access)
    if scope is None:
        raise ForbiddenError("Caller lacks metrics access for this evaluation.")
    viewer_id = caller.id if scope == MetricsScope.PERSONAL else None
    mask_model_names = evaluation.mask_models_enabled and not context.access.has(
        Permission.EVALUATION_GROUPS_VIEW_METRICS
    )
    return EvaluationMetricsView(
        evaluation=evaluation, context=context, viewer_id=viewer_id, mask_model_names=mask_model_names
    )


EvaluationMetricsDep = Annotated[EvaluationMetricsView, Depends(require_evaluation_metrics)]
ScenarioFiltersDep = Annotated[ScenarioFilters, Depends()]
