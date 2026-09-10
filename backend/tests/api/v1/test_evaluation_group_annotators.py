"""API tests for `/api/v1/evaluation-groups/{group_id}/annotators`.

The three-branch candidate pool keys on the `reviews:annotate` capability (a
live, active role granting it, not a role name; the `annotator` role is one such
holder): a `public` group lists every global holder plus the group's in-group
holders; an `organization` group its org's global holders unioned with in-group
holders (so a deliberately-invited external reviewer stays assignable); an
`invitation_only` group its in-group holders only. Plus the manage-members gate
that guards all three (in-group `owner` or break-glass admin) and soft-delete
exclusion.
"""

from datetime import date
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import EvaluationGroup
from app.core.organizations.models import Organization
from tests.api.v1.conftest import as_user
from tests.api.v1.conftest import make_role

pytestmark = pytest.mark.integration

_GROUP = ObjectType.EVALUATION_GROUP
_PUBLIC = EvaluationGroupAccessLevel.PUBLIC
_PRIVATE = EvaluationGroupAccessLevel.INVITATION_ONLY
_ORG = EvaluationGroupAccessLevel.ORGANIZATION


async def _user(db: AsyncSession, *, role: Role | None = None, permissions: list[str] | None = None) -> User:
    role = role or await make_role(db, permissions)
    return await create_user(db, email=f"{uuid4().hex[:8]}@example.com", roles=[role])


async def _org(db: AsyncSession, *, name: str = "Acme Corp") -> Organization:
    org = Organization(name=name)
    db.add(org)
    await db.flush()
    await db.refresh(org)
    return org


async def _user_in_org(
    db: AsyncSession, org: Organization, *, role: Role | None = None, permissions: list[str] | None = None
) -> User:
    user = await _user(db, role=role, permissions=permissions)
    user.organization_id = org.id
    await db.flush()
    return user


async def _group(
    db: AsyncSession,
    *,
    owner: User,
    access_level: EvaluationGroupAccessLevel = _PRIVATE,
    organization_id: UUID | None = None,
) -> EvaluationGroup:
    group = EvaluationGroup(
        title="Engagement",
        description="A red-teaming engagement.",
        created_by_id=owner.id,
        access_level=access_level,
        organization_id=organization_id,
        status=PublicationStatus.PUBLISHED,
        start_date=date(2026, 3, 1),
    )
    db.add(group)
    await db.flush()
    await db.refresh(group)
    return group


async def _assign(db: AsyncSession, group: EvaluationGroup, user: User, role: Role) -> None:
    await grant_roles(db, _GROUP, group.id, user.id, [role])


async def _owned_group(
    db: AsyncSession,
    system_roles: dict[str, Role],
    owner: User,
    *,
    access_level: EvaluationGroupAccessLevel = _PRIVATE,
    organization_id: UUID | None = None,
) -> EvaluationGroup:
    """A group whose creator holds the in-group `owner` role (so they may manage it)."""
    group = await _group(db, owner=owner, access_level=access_level, organization_id=organization_id)
    await _assign(db, group, owner, system_roles["owner"])
    return group


def _url(group: EvaluationGroup) -> str:
    return f"/api/v1/evaluation-groups/{group.id}/annotators"


# --- public group: platform-wide annotator pool -----------------------------


async def test_public_group_lists_all_global_annotators_including_non_members(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner, access_level=_PUBLIC)
    annotator_a = await _user(db_session, role=system_roles["annotator"])
    annotator_b = await _user(db_session, role=system_roles["annotator"])
    await _user(db_session, role=system_roles["red_teamer"])  # not an annotator

    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    # Both annotators appear though neither is a member of the group; the
    # red_teamer and the owner (a throwaway role) do not.
    assert body["total"] == 2
    assert {item["id"] for item in body["items"]} == {str(annotator_a.id), str(annotator_b.id)}


async def test_public_unions_global_annotators_with_in_group_members(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The public branch unions in-group holders, like organization: a member granted the
    # in-group annotator role is assignable even though their global role grants nothing.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner, access_level=_PUBLIC)
    global_annotator = await _user(db_session, role=system_roles["annotator"])
    in_group_only = await _user(db_session, role=system_roles["red_teamer"])
    await _assign(db_session, group, in_group_only, system_roles["annotator"])

    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 2
    assert {item["id"] for item in body["items"]} == {str(global_annotator.id), str(in_group_only.id)}


async def test_annotator_search_param_threads_through(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Wiring representative: the route parses `?search=` and threads it into the picker
    # (the per-access-level filtering branches are covered in the service test).
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner, access_level=_PUBLIC)
    alice = await create_user(db_session, email="ann-search-alice@example.com", roles=[system_roles["annotator"]])
    await create_user(db_session, email="ann-search-bob@example.com", roles=[system_roles["annotator"]])

    with as_user(owner):
        response = await async_client_with_db.get(f"{_url(group)}?search=alice")

    assert response.status_code == status.HTTP_200_OK
    assert {item["id"] for item in response.json()["items"]} == {str(alice.id)}


async def test_annotator_search_over_max_length_is_422_not_500(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # An over-long search must be rejected at the route (422). Without the route-level max_length,
    # the PUBLIC branch's UserFilters raises a raw pydantic ValidationError inside the service,
    # which the catch-all turns into a 500 (and only on PUBLIC groups — a branch-dependent 500).
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner, access_level=_PUBLIC)

    with as_user(owner):
        response = await async_client_with_db.get(f"{_url(group)}?search={'a' * 401}")

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_soft_deleted_annotator_excluded(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner, access_level=_PUBLIC)
    live = await _user(db_session, role=system_roles["annotator"])
    gone = await _user(db_session, role=system_roles["annotator"])
    gone.soft_delete(None)
    db_session.add(gone)
    await db_session.flush()

    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert {item["id"] for item in body["items"]} == {str(live.id)}


# --- invitation-only group: in-group annotator members only -----------------


async def test_invitation_only_lists_only_member_annotators(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    member_annotator = await _user(db_session)
    await _assign(db_session, group, member_annotator, system_roles["annotator"])
    member_red_teamer = await _user(db_session)
    await _assign(db_session, group, member_red_teamer, system_roles["red_teamer"])
    await _user(db_session, role=system_roles["annotator"])  # global annotator, not a member

    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    # Only the in-group annotator: the in-group red_teamer and the non-member
    # global annotator are both excluded.
    assert body["total"] == 1
    assert {item["id"] for item in body["items"]} == {str(member_annotator.id)}


async def test_invitation_only_excludes_soft_deleted_member(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    live = await _user(db_session)
    await _assign(db_session, group, live, system_roles["annotator"])
    gone = await _user(db_session)
    await _assign(db_session, group, gone, system_roles["annotator"])
    gone.soft_delete(None)
    db_session.add(gone)
    await db_session.flush()

    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    # A soft-deleted member keeps a live assignment, but is excluded from both
    # total and items — no `total > len(items)` skew in the picker.
    assert body["total"] == 1
    assert {item["id"] for item in body["items"]} == {str(live.id)}


# --- organization group: org annotators + in-group annotator members --------


async def test_organization_unions_org_annotators_with_in_group_members(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    org = await _org(db_session)
    owner = await _user_in_org(db_session, org)
    group = await _owned_group(db_session, system_roles, owner, access_level=_ORG, organization_id=org.id)
    # (1) org user holding the global annotator role, not an in-group member — included.
    org_annotator = await _user_in_org(db_session, org, role=system_roles["annotator"])
    # (2) in-group annotator member invited from ANOTHER org — included (membership
    #     overrides org scope, so a deliberately-invited external reviewer stays in).
    other_org = await _org(db_session, name="Globex")
    invited_external = await _user_in_org(db_session, other_org)
    await _assign(db_session, group, invited_external, system_roles["annotator"])
    # (3) org user without the annotator role — excluded.
    await _user_in_org(db_session, org)
    # (4) global annotator outside the org and not a member — excluded.
    await _user(db_session, role=system_roles["annotator"])

    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 2
    assert {item["id"] for item in body["items"]} == {str(org_annotator.id), str(invited_external.id)}


async def test_organization_excludes_soft_deleted_org_annotator(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    org = await _org(db_session)
    owner = await _user_in_org(db_session, org)
    group = await _owned_group(db_session, system_roles, owner, access_level=_ORG, organization_id=org.id)
    live = await _user_in_org(db_session, org, role=system_roles["annotator"])
    gone = await _user_in_org(db_session, org, role=system_roles["annotator"])
    gone.soft_delete(None)
    db_session.add(gone)
    await db_session.flush()

    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert {item["id"] for item in body["items"]} == {str(live.id)}


# --- pagination -------------------------------------------------------------


async def test_pagination_pages_through_annotators(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner, access_level=_PUBLIC)
    expected = {str((await _user(db_session, role=system_roles["annotator"])).id) for _ in range(3)}

    with as_user(owner):
        first = await async_client_with_db.get(_url(group), params={"limit": 2, "offset": 0})
        second = await async_client_with_db.get(_url(group), params={"limit": 2, "offset": 2})

    assert first.status_code == status.HTTP_200_OK
    assert second.status_code == status.HTTP_200_OK
    first_body, second_body = first.json(), second.json()
    assert first_body["total"] == 3
    assert len(first_body["items"]) == 2
    assert len(second_body["items"]) == 1
    paged = {item["id"] for item in first_body["items"]} | {item["id"] for item in second_body["items"]}
    assert paged == expected


async def test_limit_over_max_rejected(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner, access_level=_PUBLIC)

    with as_user(owner):
        response = await async_client_with_db.get(_url(group), params={"limit": 101})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


# --- gate: manage-members --------------------------------------------------


async def test_member_with_lesser_role_forbidden(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    member = await _user(db_session)
    await _assign(db_session, group, member, system_roles["red_teamer"])

    with as_user(member):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_non_member_gets_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    outsider = await _user(db_session)

    with as_user(outsider):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_break_glass_admin_lists_without_membership(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    member_annotator = await _user(db_session)
    await _assign(db_session, group, member_annotator, system_roles["annotator"])
    admin = await _user(db_session, permissions=[Permission.EVALUATION_GROUPS_MANAGE.value])

    with as_user(admin):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert {item["id"] for item in body["items"]} == {str(member_annotator.id)}
