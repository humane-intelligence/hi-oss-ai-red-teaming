"""API tests for `/api/v1/evaluation-groups/{group_id}/assignable-roles`.

The manage-members gate (in-group `owner` or break-glass admin, no global
`roles:read` needed) and the listed set, which has to match what the member add
accepts: live, active, object-assignable roles granting `evaluation_groups:read`.
"""

from datetime import UTC
from datetime import date
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import OBJECT_ROLE_REGISTRY
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.object_roles.service import resolve_assignable_roles
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import EvaluationGroup
from app.core.exceptions import BadRequestError
from tests.api.v1.conftest import as_user
from tests.api.v1.conftest import make_role

pytestmark = pytest.mark.integration

_GROUP_READ = Permission.EVALUATION_GROUPS_READ.value


async def _user(db: AsyncSession, role: Role | None = None) -> User:
    role = role or await make_role(db)
    return await create_user(db, email=f"{uuid4().hex[:8]}@example.com", roles=[role])


async def _owned_group(db: AsyncSession, system_roles: dict[str, Role], owner: User) -> EvaluationGroup:
    """An invitation-only group whose creator holds the in-group `owner` role."""
    group = EvaluationGroup(
        title="Engagement",
        description="A red-teaming engagement.",
        created_by_id=owner.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        start_date=date(2026, 3, 1),
    )
    db.add(group)
    await db.flush()
    await db.refresh(group)
    await grant_roles(db, ObjectType.EVALUATION_GROUP, group.id, owner.id, [system_roles["owner"]])
    return group


async def _custom_role(db: AsyncSession, name: str, *, permissions: list[str], **flags: object) -> Role:
    role = Role(name=name, display_name=name.title(), permissions=permissions, is_object_assignable=True)
    for field, value in flags.items():
        setattr(role, field, value)
    db.add(role)
    await db.flush()
    return role


def _url(group: EvaluationGroup) -> str:
    return f"/api/v1/evaluation-groups/{group.id}/assignable-roles"


async def test_in_group_owner_without_roles_read_lists_the_assignable_roles(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session, system_roles["red_teamer"])
    assert Permission.ROLES_READ.value not in system_roles["red_teamer"].permissions
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [role["name"] for role in body["items"]] == ["annotator", "owner", "red_teamer", "viewer"]
    assert body["total"] == 4
    assert body["items"][2] == {
        "id": str(system_roles["red_teamer"].id),
        "name": "red_teamer",
        "display_name": system_roles["red_teamer"].label,
        "is_participant_default": True,
    }


async def test_lists_only_what_a_member_add_accepts(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session, system_roles["red_teamer"])
    group = await _owned_group(db_session, system_roles, owner)
    offered = await _custom_role(db_session, "custom_offered", permissions=[_GROUP_READ])
    await _custom_role(db_session, "custom_no_read", permissions=[])
    await _custom_role(db_session, "custom_inactive", permissions=[_GROUP_READ], is_active=False)
    await _custom_role(db_session, "custom_platform_only", permissions=[_GROUP_READ], is_object_assignable=False)
    await _custom_role(db_session, "custom_deleted", permissions=[_GROUP_READ], deleted_at=datetime.now(UTC))

    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    custom = [role for role in response.json()["items"] if role["name"].startswith("custom_")]
    assert custom == [
        {
            "id": str(offered.id),
            "name": "custom_offered",
            "display_name": "Custom_Offered",
            "is_participant_default": False,
        }
    ]


async def test_lists_a_role_exactly_when_assigning_it_is_accepted(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session, system_roles["red_teamer"])
    group = await _owned_group(db_session, system_roles, owner)
    await _custom_role(db_session, "custom_offered", permissions=[_GROUP_READ])
    await _custom_role(db_session, "custom_no_read", permissions=[])
    await _custom_role(db_session, "custom_inactive", permissions=[_GROUP_READ], is_active=False)
    await _custom_role(db_session, "custom_platform_only", permissions=[_GROUP_READ], is_object_assignable=False)
    roles = list((await db_session.execute(Role.live_select())).scalars().all())
    spec = OBJECT_ROLE_REGISTRY[ObjectType.EVALUATION_GROUP]

    accepted = set()
    for role in roles:
        try:
            await resolve_assignable_roles(db_session, spec, [role.id])
        except BadRequestError:
            continue
        accepted.add(role.name)
    with as_user(owner):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    assert {role["name"] for role in response.json()["items"]} == accepted
    assert "custom_offered" in accepted


async def test_pages_through_the_roles(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session, system_roles["red_teamer"])
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.get(_url(group), params={"limit": 2, "offset": 2})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [role["name"] for role in body["items"]] == ["red_teamer", "viewer"]
    assert (body["total"], body["limit"], body["offset"]) == (4, 2, 2)


async def test_break_glass_admin_lists_roles_without_membership(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    admin = await _user(db_session, system_roles["admin"])

    with as_user(admin):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_200_OK
    assert "admin" not in [role["name"] for role in response.json()["items"]]


async def test_member_with_lesser_role_gets_403(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    member = await _user(db_session, system_roles["red_teamer"])
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, member.id, [system_roles["red_teamer"]])

    with as_user(member):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_hidden_group_is_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    outsider = await _user(db_session, system_roles["red_teamer"])

    with as_user(outsider):
        response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_requires_a_bearer_token(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)

    response = await async_client_with_db.get(_url(group))

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


async def test_rejects_a_page_over_the_cap(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session, system_roles["red_teamer"])
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.get(_url(group), params={"limit": 101})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
