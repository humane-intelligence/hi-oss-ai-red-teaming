"""Annotator search for flag/review assignment within an evaluation group.

The candidate pool is defined by capability, not role name: a user is assignable
when they hold a live, active role granting `reviews:annotate` — globally (via
`UserRole`) or as an in-group object role (via `ObjectRoleAssignment`). So a
custom role that grants the permission joins the pool, and deactivating a role
drops its holders. In-group holders are always assignable (membership is the
object-role escape hatch); the access level only scopes the *global* arm:

- **`public`** — every global holder of `reviews:annotate` (anyone may join, so
  anyone may be assigned), plus the group's in-group holders.
- **`organization`** — the group org's own global holders (org members may
  self-join), plus the group's in-group holders — so a deliberately-invited
  *external* reviewer (cross-org, granted an in-group role) stays assignable.
- **`invitation_only`** — in-group holders only (no global arm).

The visibility/permission gate lives at the route (`ManageMembersDep`), so this
layer takes the resolved group as given.
"""

from uuid import UUID

from sqlalchemy import ColumnElement
from sqlalchemy import Select
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserRole
from app.core.auth.object_roles.models import ObjectRoleAssignment
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.roles import Permission
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import EvaluationGroup
from app.core.helpers import escape_like
from app.core.pagination import paginate


def _reviewer_role_ids() -> Select[tuple[UUID]]:
    """Ids of live, active roles that grant the reviewer (`reviews:annotate`) permission."""
    return select(col(Role.id)).where(
        col(Role.deleted_at).is_(None),
        col(Role.is_active).is_(True),
        col(Role.permissions).contains([Permission.REVIEWS_ANNOTATE.value]),
    )


def _global_reviewer_ids() -> Select[tuple[UUID]]:
    """User ids holding a platform-wide role that grants `reviews:annotate`."""
    return select(col(UserRole.user_id)).where(col(UserRole.role_id).in_(_reviewer_role_ids()))


def _member_reviewer_ids(group_id: UUID) -> Select[tuple[UUID]]:
    """User ids holding a live in-group role granting `reviews:annotate` on `group_id`."""
    return select(col(ObjectRoleAssignment.user_id)).where(
        col(ObjectRoleAssignment.object_type) == ObjectType.EVALUATION_GROUP,
        col(ObjectRoleAssignment.object_id) == group_id,
        col(ObjectRoleAssignment.role_id).in_(_reviewer_role_ids()),
        col(ObjectRoleAssignment.deleted_at).is_(None),
    )


def _organization_pool_predicate(group: EvaluationGroup) -> ColumnElement[bool]:
    """`User`-row predicate for an `organization` group's reviewer pool.

    In-group holders, plus the group org's own global `reviews:annotate` holders.
    The org arm drops out when `organization_id` is missing — an `organization`
    group always has one, but guard the `None` so a stray value can't collapse to
    `organization_id IS NULL` and match every orgless user.
    """
    is_member = col(User.id).in_(_member_reviewer_ids(group.id))
    if group.organization_id is None:
        return is_member
    is_org_reviewer = and_(
        col(User.organization_id) == group.organization_id,
        col(User.id).in_(_global_reviewer_ids()),
    )
    return or_(is_member, is_org_reviewer)


def annotator_pool_predicate(group: EvaluationGroup) -> ColumnElement[bool]:
    """`User`-row membership predicate for `group`'s reviewer pool, per access level.

    `public` admits every global `reviews:annotate` holder **plus** the group's
    in-group holders; `organization` delegates to `_organization_pool_predicate`
    (org-scoped global arm plus in-group); `invitation_only` admits in-group holders
    only. `is_assignable_annotator` and `list_group_annotators` share this, so the
    picker and the gate stay in lockstep, including on soft-deleted users.
    """
    if group.access_level == EvaluationGroupAccessLevel.PUBLIC:
        return or_(col(User.id).in_(_global_reviewer_ids()), col(User.id).in_(_member_reviewer_ids(group.id)))
    if group.access_level == EvaluationGroupAccessLevel.ORGANIZATION:
        return _organization_pool_predicate(group)
    return col(User.id).in_(_member_reviewer_ids(group.id))


async def list_group_annotators(
    session: AsyncSession,
    group: EvaluationGroup,
    *,
    search: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[User], int]:
    """Return one page of reviewers assignable to a flag/review in `group`.

    Membership is `annotator_pool_predicate` (per access level); ordered by email
    then id on every branch, matching the reviews assign picker (`reviews.py`).
    `search` is a case-insensitive substring over email / first / last name.
    """
    statement = User.live_select().where(annotator_pool_predicate(group))
    if search:
        like = f"%{escape_like(search)}%"
        statement = statement.where(
            or_(
                col(User.email).ilike(like, escape="\\"),
                col(User.first_name).ilike(like, escape="\\"),
                col(User.last_name).ilike(like, escape="\\"),
            )
        )
    statement = statement.order_by(col(User.email), col(User.id))
    return await paginate(session, statement, limit=limit, offset=offset)


async def is_assignable_annotator(session: AsyncSession, group: EvaluationGroup, user_id: UUID) -> bool:
    """Whether `user_id` is in `group`'s assignable-reviewer pool.

    The membership-test counterpart of `list_group_annotators`: runs the shared
    `annotator_pool_predicate` for the single user under `User.live_select()`, so
    the gate accepts exactly the live users the picker would page (including the
    soft-deleted-user exclusion) — the picker and the gate cannot disagree.
    """
    found = await session.scalar(User.live_select().where(col(User.id) == user_id, annotator_pool_predicate(group)))
    return found is not None
