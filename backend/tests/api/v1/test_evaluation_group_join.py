"""API tests for `POST /api/v1/evaluation-groups/{group_id}/join`.

Self-service join: a caller adds *themselves* to a self-joinable, `published`
group as a `red_teamer`. The matrix covers the access-mode gate (`public` and
`organization` joinable, `invitation_only` not — and invisible private groups
read as 404), the org-scoped join (an org member joins their org's group, an
outsider can't even see it), the lifecycle guard (only `published`), and the
already-a-member conflict (owners included).
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

_READ = [Permission.EVALUATION_GROUPS_READ.value]


async def _user(db: AsyncSession, permissions: list[str] | None = None) -> User:
    role = await make_role(db, permissions)
    return await create_user(db, email=f"{uuid4().hex[:8]}@example.com", roles=[role])


async def _org(db: AsyncSession, *, name: str = "Acme Corp") -> Organization:
    org = Organization(name=name)
    db.add(org)
    await db.flush()
    await db.refresh(org)
    return org


async def _user_in_org(db: AsyncSession, org: Organization, permissions: list[str] | None = None) -> User:
    user = await _user(db, permissions=permissions)
    # Set the FK and flush only — `refresh` would expire the `roles` collection
    # that `create_user` loaded and make `session_user_from` (sync, in `as_user`)
    # lazy-load it on an async session.
    user.organization_id = org.id
    await db.flush()
    return user


async def _group(
    db: AsyncSession,
    *,
    owner: User,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
    organization_id: UUID | None = None,
    group_status: PublicationStatus = PublicationStatus.PUBLISHED,
) -> EvaluationGroup:
    group = EvaluationGroup(
        title="Engagement",
        description="A red-teaming engagement.",
        created_by_id=owner.id,
        access_level=access_level,
        organization_id=organization_id,
        status=group_status,
        start_date=date(2026, 3, 1),
    )
    db.add(group)
    await db.flush()
    await db.refresh(group)
    return group


def _join_url(group: EvaluationGroup) -> str:
    return f"/api/v1/evaluation-groups/{group.id}/join"


async def test_join_public_published_group_adds_caller_as_red_teamer(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _group(db_session, owner=owner)
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, owner.id, [system_roles["owner"]])
    joiner = await _user(db_session, permissions=_READ)

    with as_user(joiner):
        response = await async_client_with_db.post(_join_url(group))

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["user"]["id"] == str(joiner.id)
    assert [role["name"] for role in body["roles"]] == ["red_teamer"]


async def test_join_organization_group_in_own_org_adds_caller_as_red_teamer(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    org = await _org(db_session)
    owner = await _user_in_org(db_session, org)
    group = await _group(
        db_session, owner=owner, access_level=EvaluationGroupAccessLevel.ORGANIZATION, organization_id=org.id
    )
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, owner.id, [system_roles["owner"]])
    joiner = await _user_in_org(db_session, org, permissions=_READ)

    with as_user(joiner):
        response = await async_client_with_db.post(_join_url(group))

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["user"]["id"] == str(joiner.id)
    assert [role["name"] for role in body["roles"]] == ["red_teamer"]


async def test_join_grants_the_participant_default_role_not_a_hardcoded_name(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Self-join follows the is_participant_default flag, not a fixed slug: move the
    # flag to another role and the granted role moves with it.
    owner = await _user(db_session)
    group = await _group(db_session, owner=owner)
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, owner.id, [system_roles["owner"]])

    # Clear before set — the partial-unique index allows one live participant default.
    system_roles["red_teamer"].is_participant_default = False
    db_session.add(system_roles["red_teamer"])
    await db_session.flush()
    custom = await make_role(db_session, _READ)
    custom.is_participant_default = True
    # Self-join grants an object role, so the participant default must be one.
    custom.is_object_assignable = True
    db_session.add(custom)
    await db_session.flush()

    joiner = await _user(db_session, permissions=_READ)
    with as_user(joiner):
        response = await async_client_with_db.post(_join_url(group))

    assert response.status_code == status.HTTP_201_CREATED
    assert [role["name"] for role in response.json()["roles"]] == [custom.name]


async def test_join_organization_group_outside_caller_org_is_not_found(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # An organization group is invisible outside its org, so an outsider gets 404
    # (never leaking its existence) rather than the invitation-only 403.
    org = await _org(db_session)
    other_org = await _org(db_session, name="Globex")
    owner = await _user_in_org(db_session, org)
    group = await _group(
        db_session, owner=owner, access_level=EvaluationGroupAccessLevel.ORGANIZATION, organization_id=org.id
    )
    outsider = await _user_in_org(db_session, other_org, permissions=_READ)

    with as_user(outsider):
        response = await async_client_with_db.post(_join_url(group))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_join_invitation_only_group_is_forbidden_for_visible_member(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A viewer can *see* the invitation-only group, but self-join is still barred.
    owner = await _user(db_session)
    group = await _group(db_session, owner=owner, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    member = await _user(db_session, permissions=_READ)
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, [system_roles["viewer"]])

    with as_user(member):
        response = await async_client_with_db.post(_join_url(group))

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_join_invisible_invitation_only_group_is_not_found(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _user(db_session)
    group = await _group(db_session, owner=owner, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    outsider = await _user(db_session, permissions=_READ)

    with as_user(outsider):
        response = await async_client_with_db.post(_join_url(group))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_join_unpublished_group_conflicts(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # A `public` group that is visible but not yet `published` (here
    # `pending_approval`) reaches the published gate → 409. A `public` *draft* is a
    # different case: it isn't visible at all, so it 404s (see the test below).
    owner = await _user(db_session)
    group = await _group(db_session, owner=owner, group_status=PublicationStatus.PENDING_APPROVAL)
    joiner = await _user(db_session, permissions=_READ)

    with as_user(joiner):
        response = await async_client_with_db.post(_join_url(group))

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_join_public_draft_is_not_found(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    # A `public` draft is owner/manager-only, so a non-member can't see it
    # to join — the visibility gate 404s before the published check is reached.
    owner = await _user(db_session)
    group = await _group(db_session, owner=owner, group_status=PublicationStatus.DRAFT)
    joiner = await _user(db_session, permissions=_READ)

    with as_user(joiner):
        response = await async_client_with_db.post(_join_url(group))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_join_when_already_member_conflicts(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _group(db_session, owner=owner)
    joiner = await _user(db_session, permissions=_READ)
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, joiner.id, [system_roles["red_teamer"]])

    with as_user(joiner):
        response = await async_client_with_db.post(_join_url(group))

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_owner_joining_own_public_group_conflicts(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session, permissions=_READ)
    group = await _group(db_session, owner=owner)
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, owner.id, [system_roles["owner"]])

    with as_user(owner):
        response = await async_client_with_db.post(_join_url(group))

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_join_without_read_permission_is_forbidden(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _user(db_session)
    group = await _group(db_session, owner=owner)
    joiner = await _user(db_session, permissions=[])

    with as_user(joiner):
        response = await async_client_with_db.post(_join_url(group))

    assert response.status_code == status.HTTP_403_FORBIDDEN
