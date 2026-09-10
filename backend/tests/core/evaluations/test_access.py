"""Unit tests for the pure metrics-access policy (`resolve_metrics_scope`).

The route-level 401/403/404 behavior (and the visibility gate the policy assumes
ran first) is covered by the API tests; this is the in-memory truth table:
level x caller relationship x lifecycle phase, mapping each to a `MetricsScope`
(`FULL` / `PERSONAL`) or `None` (denied). Member contexts derive their permission
sets from the canonical `ROLE_PERMISSIONS`, so the matrix also pins the role→grant
mapping the policy relies on (only `red_teamer` carrying
`evaluation_groups:view_personal_metrics` is what admits it at
`members_personal_metrics` — never a role-name check).
"""

from uuid import uuid4

import pytest

from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import ObjectAccessContext
from app.core.auth.roles import ROLE_PERMISSIONS
from app.core.auth.roles import SystemRole
from app.core.evaluations.access import resolve_metrics_scope
from app.core.evaluations.enums import MetricsAccessLevel
from app.core.evaluations.enums import MetricsScope
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import EvaluationGroup

pytestmark = pytest.mark.unit


def _group(
    *,
    status: PublicationStatus = PublicationStatus.PUBLISHED,
    during: MetricsAccessLevel = MetricsAccessLevel.OWNER_ONLY,
    after: MetricsAccessLevel = MetricsAccessLevel.OWNER_ONLY,
) -> EvaluationGroup:
    return EvaluationGroup(
        title="Engagement",
        created_by_id=uuid4(),
        status=status,
        metrics_access_during=during,
        metrics_access_after=after,
    )


def _access(
    *,
    permissions: frozenset[str] = frozenset(),
    is_member: bool = False,
    has_super: bool = False,
) -> ObjectAccessContext:
    return ObjectAccessContext(
        object_type=ObjectType.EVALUATION_GROUP,
        object_id=uuid4(),
        permissions=permissions,
        is_member=is_member,
        has_super=has_super,
    )


def _member(*roles: SystemRole) -> ObjectAccessContext:
    """A member whose object permissions are the union of the given canonical roles' grants."""
    permissions = frozenset(perm for role in roles for perm in ROLE_PERMISSIONS[role])
    return _access(permissions=permissions, is_member=True)


_OWNER = _member(SystemRole.OWNER)
_ADMIN = _access(has_super=True)
_NON_MEMBER = _access()


@pytest.mark.parametrize("level", list(MetricsAccessLevel))
@pytest.mark.parametrize("access", [_OWNER, _ADMIN], ids=["owner", "break-glass-admin"])
def test_owner_and_admin_get_full_at_every_level(level: MetricsAccessLevel, access: ObjectAccessContext) -> None:
    group = _group(during=level, after=level)

    assert resolve_metrics_scope(group=group, access=access) is MetricsScope.FULL


@pytest.mark.parametrize(
    ("level", "access", "expected"),
    [
        # inherit_group_access — whoever can see the group gets the full aggregate.
        (MetricsAccessLevel.INHERIT_GROUP_ACCESS, _NON_MEMBER, MetricsScope.FULL),
        (MetricsAccessLevel.INHERIT_GROUP_ACCESS, _member(SystemRole.ANNOTATOR), MetricsScope.FULL),
        # all_members — any member gets the full aggregate; a non-member is denied.
        (MetricsAccessLevel.ALL_MEMBERS, _NON_MEMBER, None),
        (MetricsAccessLevel.ALL_MEMBERS, _member(SystemRole.RED_TEAMER), MetricsScope.FULL),
        (MetricsAccessLevel.ALL_MEMBERS, _member(SystemRole.ANNOTATOR), MetricsScope.FULL),
        (MetricsAccessLevel.ALL_MEMBERS, _member(SystemRole.VIEWER), MetricsScope.FULL),
        # members_personal_metrics — a member holding view_personal_metrics gets a PERSONAL read.
        (MetricsAccessLevel.MEMBERS_PERSONAL_METRICS, _NON_MEMBER, None),
        (MetricsAccessLevel.MEMBERS_PERSONAL_METRICS, _member(SystemRole.RED_TEAMER), MetricsScope.PERSONAL),
        # The viewer role does NOT carry view_personal_metrics (it authors nothing),
        # the annotator role does not either — that missing grant, not the role name, excludes them.
        (MetricsAccessLevel.MEMBERS_PERSONAL_METRICS, _member(SystemRole.VIEWER), None),
        (MetricsAccessLevel.MEMBERS_PERSONAL_METRICS, _member(SystemRole.ANNOTATOR), None),
        # A multi-role member qualifies via the permission union.
        (
            MetricsAccessLevel.MEMBERS_PERSONAL_METRICS,
            _member(SystemRole.ANNOTATOR, SystemRole.RED_TEAMER),
            MetricsScope.PERSONAL,
        ),
        # owner_only — nobody beyond the view_metrics holders above.
        (MetricsAccessLevel.OWNER_ONLY, _NON_MEMBER, None),
        (MetricsAccessLevel.OWNER_ONLY, _member(SystemRole.RED_TEAMER), None),
        (MetricsAccessLevel.OWNER_ONLY, _member(SystemRole.ANNOTATOR), None),
        (MetricsAccessLevel.OWNER_ONLY, _member(SystemRole.VIEWER), None),
    ],
)
def test_level_matrix_on_an_active_group(
    level: MetricsAccessLevel, access: ObjectAccessContext, expected: MetricsScope | None
) -> None:
    group = _group(during=level, after=MetricsAccessLevel.OWNER_ONLY)

    assert resolve_metrics_scope(group=group, access=access) is expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        # `after` (inherit_group_access here) applies only once the group is
        # `inactive`; every other status — including the pre-published ones —
        # reads `during` (owner_only here). Visibility confines pre-published
        # groups anyway.
        (PublicationStatus.DRAFT, None),
        (PublicationStatus.PENDING_APPROVAL, None),
        (PublicationStatus.CHANGES_REQUESTED, None),
        (PublicationStatus.APPROVED, None),
        (PublicationStatus.NOT_APPROVED, None),
        (PublicationStatus.PUBLISHED, None),
        (PublicationStatus.INACTIVE, MetricsScope.FULL),
    ],
)
def test_after_level_applies_only_when_inactive(status: PublicationStatus, expected: MetricsScope | None) -> None:
    group = _group(status=status, during=MetricsAccessLevel.OWNER_ONLY, after=MetricsAccessLevel.INHERIT_GROUP_ACCESS)

    assert resolve_metrics_scope(group=group, access=_NON_MEMBER) is expected
