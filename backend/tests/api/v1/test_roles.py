"""API tests for the role catalog + management endpoints on `/api/v1/roles`."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import Role
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.services.roles import create_role
from app.core.auth.services.roles import set_role_active
from app.core.auth.services.roles import update_role
from app.core.auth.services.users import create_user
from tests.api.v1.conftest import PROBLEM_CT
from tests.api.v1.conftest import bearer
from tests.api.v1.conftest import caller_with

pytestmark = pytest.mark.integration


async def test_list_roles_hides_inactive_by_default(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    reader = await caller_with(db_session, Permission.ROLES_READ)
    await set_role_active(db_session, system_roles[SystemRole.VIEWER.value], is_active=False)

    default_view = await auth_db_client.get("/api/v1/roles", headers=bearer(reader))
    full_view = await auth_db_client.get("/api/v1/roles?include_inactive=true", headers=bearer(reader))

    assert default_view.status_code == status.HTTP_200_OK
    default_names = {item["name"] for item in default_view.json()["items"]}
    full_names = {item["name"] for item in full_view.json()["items"]}
    assert SystemRole.VIEWER.value not in default_names
    assert SystemRole.VIEWER.value in full_names


async def test_list_roles_filters_by_object_assignability(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    reader = await caller_with(db_session, Permission.ROLES_READ)
    # A custom role opted in, and one left global-only.
    in_group = await create_role(
        db_session,
        name="in-group",
        display_name="In Group",
        description=None,
        permissions=[Permission.EVALUATION_GROUPS_READ.value],
    )
    await update_role(db_session, in_group, is_object_assignable=True)
    await create_role(db_session, name="global-only", display_name="Global Only", description=None, permissions=[])

    assignable = await auth_db_client.get("/api/v1/roles?is_object_assignable=true", headers=bearer(reader))
    rest = await auth_db_client.get("/api/v1/roles?is_object_assignable=false", headers=bearer(reader))

    assert assignable.status_code == status.HTTP_200_OK
    assignable_names = {item["name"] for item in assignable.json()["items"]}
    rest_names = {item["name"] for item in rest.json()["items"]}
    # The four in-group system roles plus the opted-in custom one; admin and the
    # global-only custom role fall on the other side — as does the caller's own
    # throwaway role, so the complement is a superset check.
    assert assignable_names == {
        SystemRole.OWNER.value,
        SystemRole.RED_TEAMER.value,
        SystemRole.ANNOTATOR.value,
        SystemRole.VIEWER.value,
        "in-group",
    }
    assert rest_names >= {SystemRole.ADMIN.value, "global-only"}
    assert "in-group" not in rest_names
    assert all(item["is_object_assignable"] for item in assignable.json()["items"])
    assert not any(item["is_object_assignable"] for item in rest.json()["items"])


async def test_list_roles_unfiltered_returns_the_whole_catalog(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    reader = await caller_with(db_session, Permission.ROLES_READ)

    response = await auth_db_client.get("/api/v1/roles?limit=100", headers=bearer(reader))

    names = {item["name"] for item in response.json()["items"]}
    assert {role.value for role in SystemRole} <= names


async def test_get_role_returns_the_role(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    reader = await caller_with(db_session, Permission.ROLES_READ)
    role = await create_role(db_session, name="fetch_me", display_name="Fetch Me", description=None, permissions=[])

    response = await auth_db_client.get(f"/api/v1/roles/{role.id}", headers=bearer(reader))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "fetch_me"


async def test_get_role_returns_an_inactive_role(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    reader = await caller_with(db_session, Permission.ROLES_READ)
    role = await create_role(db_session, name="dormant", display_name="Dormant", description=None, permissions=[])
    await set_role_active(db_session, role, is_active=False)

    response = await auth_db_client.get(f"/api/v1/roles/{role.id}", headers=bearer(reader))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["is_active"] is False


async def test_get_role_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    reader = await caller_with(db_session, Permission.ROLES_READ)

    response = await auth_db_client.get(f"/api/v1/roles/{uuid4()}", headers=bearer(reader))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"] == PROBLEM_CT


async def test_create_role_returns_201(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)

    response = await auth_db_client.post(
        "/api/v1/roles",
        headers=bearer(manager),
        json={
            "name": "lead_reviewer",
            "display_name": "Lead Reviewer",
            "permissions": [Permission.REVIEWS_READ.value, Permission.REVIEWS_ANNOTATE.value],
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["name"] == "lead_reviewer"
    assert (body["is_system"], body["is_active"], body["is_default"]) == (False, True, False)
    assert response.headers["Location"] == f"/api/v1/roles/{body['id']}"


async def test_create_role_requires_roles_manage(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    reader = await caller_with(db_session, Permission.ROLES_READ)

    response = await auth_db_client.post(
        "/api/v1/roles",
        headers=bearer(reader),
        json={"name": "nope", "display_name": "Nope", "permissions": []},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"] == PROBLEM_CT


async def test_create_role_rejects_unknown_permission(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)

    response = await auth_db_client.post(
        "/api/v1/roles",
        headers=bearer(manager),
        json={"name": "bad", "display_name": "Bad", "permissions": ["not:a:perm"]},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"] == PROBLEM_CT
    assert "not:a:perm" in response.json()["detail"]


async def test_create_role_rejects_non_delegable_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)

    response = await auth_db_client.post(
        "/api/v1/roles",
        headers=bearer(manager),
        json={"name": "sneaky", "display_name": "Sneaky", "permissions": [Permission.ROLES_MANAGE.value]},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_create_role_rejects_reserved_name(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)

    response = await auth_db_client.post(
        "/api/v1/roles",
        headers=bearer(manager),
        json={"name": SystemRole.ADMIN.value, "display_name": "Admin", "permissions": []},
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"] == PROBLEM_CT


async def test_create_role_rejects_invalid_name_pattern(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)

    response = await auth_db_client.post(
        "/api/v1/roles",
        headers=bearer(manager),
        json={"name": "Bad Name!", "display_name": "Bad", "permissions": []},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.json()["errors"]


async def test_create_role_rejects_duplicate_name(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    payload = {"name": "dup", "display_name": "Dup", "permissions": []}

    first = await auth_db_client.post("/api/v1/roles", headers=bearer(manager), json=payload)
    second = await auth_db_client.post("/api/v1/roles", headers=bearer(manager), json=payload)

    assert first.status_code == status.HTTP_201_CREATED
    assert second.status_code == status.HTTP_409_CONFLICT


async def test_update_role_edits_custom_fields(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(
        db_session, name="editme", display_name="Editme", description="old", permissions=[Permission.MODELS_READ.value]
    )

    response = await auth_db_client.patch(
        f"/api/v1/roles/{role.id}",
        headers=bearer(manager),
        json={"display_name": "Renamed", "permissions": [Permission.EVALUATIONS_READ.value]},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["display_name"] == "Renamed"
    assert body["permissions"] == [Permission.EVALUATIONS_READ.value]


async def test_update_role_clears_the_description_with_an_explicit_null(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(db_session, name="clearme", display_name="Clearme", description="old", permissions=[])

    response = await auth_db_client.patch(
        f"/api/v1/roles/{role.id}", headers=bearer(manager), json={"description": None}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["description"] is None


async def test_update_role_rejects_an_explicit_null_on_a_non_nullable_field(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # `null` used to parse as "omitted", so the request was a silent no-op.
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(db_session, name="nonull", display_name="No Null", description=None, permissions=[])

    response = await auth_db_client.patch(
        f"/api/v1/roles/{role.id}", headers=bearer(manager), json={"display_name": None}
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.headers["content-type"] == PROBLEM_CT


async def test_update_role_rejects_system_field_edit(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)

    response = await auth_db_client.patch(
        f"/api/v1/roles/{system_roles[SystemRole.ADMIN.value].id}",
        headers=bearer(manager),
        json={"display_name": "Hacked"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_update_role_deactivates(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(db_session, name="toggle", display_name="Toggle", description=None, permissions=[])

    response = await auth_db_client.patch(
        f"/api/v1/roles/{role.id}", headers=bearer(manager), json={"is_active": False}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["is_active"] is False


async def test_update_role_sets_default(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(
        db_session, name="new-default", display_name="New Default", description=None, permissions=[]
    )

    response = await auth_db_client.patch(
        f"/api/v1/roles/{role.id}", headers=bearer(manager), json={"is_default": True}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["is_default"] is True


async def test_update_role_cannot_clear_default_directly(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(
        db_session, name="the-default", display_name="The Default", description=None, permissions=[]
    )
    await auth_db_client.patch(f"/api/v1/roles/{role.id}", headers=bearer(manager), json={"is_default": True})

    response = await auth_db_client.patch(
        f"/api/v1/roles/{role.id}", headers=bearer(manager), json={"is_default": False}
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"] == PROBLEM_CT


async def test_update_role_sets_participant_default(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(
        db_session,
        name="new-participant",
        display_name="New Participant",
        description=None,
        permissions=[Permission.EVALUATION_GROUPS_READ.value],
    )

    # One call: opt into in-group use, then become the participant default.
    response = await auth_db_client.patch(
        f"/api/v1/roles/{role.id}",
        headers=bearer(manager),
        json={"is_object_assignable": True, "is_participant_default": True},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["is_participant_default"] is True
    assert response.json()["is_object_assignable"] is True


async def test_update_role_sets_object_assignable(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(
        db_session,
        name="in-group",
        display_name="In Group",
        description=None,
        permissions=[Permission.EVALUATION_GROUPS_READ.value],
    )

    response = await auth_db_client.patch(
        f"/api/v1/roles/{role.id}", headers=bearer(manager), json={"is_object_assignable": True}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["is_object_assignable"] is True


async def test_update_role_rejects_dropping_group_read_from_assignable_role(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(
        db_session,
        name="drops-read",
        display_name="Drops Read",
        description=None,
        permissions=[Permission.EVALUATION_GROUPS_READ.value],
    )
    await auth_db_client.patch(f"/api/v1/roles/{role.id}", headers=bearer(manager), json={"is_object_assignable": True})

    response = await auth_db_client.patch(
        f"/api/v1/roles/{role.id}", headers=bearer(manager), json={"permissions": [Permission.SAVED_VIEWS_READ.value]}
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"] == PROBLEM_CT


async def test_update_role_rejects_object_assignable_on_system_role(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Code-owned for system roles — `sync_system_roles` re-converges it every deploy.
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)

    response = await auth_db_client.patch(
        f"/api/v1/roles/{system_roles[SystemRole.VIEWER.value].id}",
        headers=bearer(manager),
        json={"is_object_assignable": False},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"] == PROBLEM_CT


async def test_update_role_rejects_participant_default_that_is_not_group_assignable(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(
        db_session,
        name="global-participant",
        display_name="Global Participant",
        description=None,
        permissions=[Permission.EVALUATION_GROUPS_READ.value],
    )

    response = await auth_db_client.patch(
        f"/api/v1/roles/{role.id}", headers=bearer(manager), json={"is_participant_default": True}
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"] == PROBLEM_CT


async def test_update_role_cannot_clear_participant_default_directly(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(
        db_session,
        name="the-participant",
        display_name="The Participant",
        description=None,
        permissions=[Permission.EVALUATION_GROUPS_READ.value],
    )
    await auth_db_client.patch(
        f"/api/v1/roles/{role.id}",
        headers=bearer(manager),
        json={"is_object_assignable": True, "is_participant_default": True},
    )

    response = await auth_db_client.patch(
        f"/api/v1/roles/{role.id}", headers=bearer(manager), json={"is_participant_default": False}
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"] == PROBLEM_CT


@pytest.mark.parametrize("flag", ["is_default", "is_participant_default"])
async def test_update_role_rejects_admin_as_default(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role], flag: str
) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)

    response = await auth_db_client.patch(
        f"/api/v1/roles/{system_roles[SystemRole.ADMIN.value].id}", headers=bearer(manager), json={flag: True}
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"] == PROBLEM_CT


async def test_update_role_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)

    response = await auth_db_client.patch(
        f"/api/v1/roles/{uuid4()}", headers=bearer(manager), json={"display_name": "x"}
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"] == PROBLEM_CT


async def test_delete_role_returns_204(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(db_session, name="goner", display_name="Goner", description=None, permissions=[])

    response = await auth_db_client.delete(f"/api/v1/roles/{role.id}", headers=bearer(manager))

    assert response.status_code == status.HTTP_204_NO_CONTENT


async def test_delete_role_rejects_system_role(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)

    response = await auth_db_client.delete(
        f"/api/v1/roles/{system_roles[SystemRole.VIEWER.value].id}", headers=bearer(manager)
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_delete_role_rejects_sole_holder(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(db_session, name="del-sole", display_name="Del Sole", description=None, permissions=[])
    await create_user(db_session, email=f"{uuid4().hex[:8]}@example.com", roles=[role])

    response = await auth_db_client.delete(f"/api/v1/roles/{role.id}", headers=bearer(manager))

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_delete_role_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)

    response = await auth_db_client.delete(f"/api/v1/roles/{uuid4()}", headers=bearer(manager))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def _backdate_role_tombstone(db_session: AsyncSession, role_id: UUID, *, days: int) -> None:
    """Age a tombstone past the restore window.

    Plain `select` (it must see deleted rows) with `populate_existing` to refresh just this
    row — a session-wide `expire_all()` would leave every `User` the test still holds
    expired, and the next attribute read would try a sync lazy-load off the async session.
    """
    statement = select(Role).where(col(Role.id) == role_id).execution_options(populate_existing=True)
    row = (await db_session.execute(statement)).scalar_one()
    row.deleted_at = datetime.now(UTC) - timedelta(days=days)
    db_session.add(row)
    await db_session.flush()


async def test_restore_role_returns_it_to_the_catalog(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE, Permission.ROLES_READ)
    role = await create_role(
        db_session,
        name="revivable",
        display_name="Revivable",
        description=None,
        permissions=[Permission.EVALUATIONS_READ.value],
    )
    role_id = role.id
    assert (await auth_db_client.delete(f"/api/v1/roles/{role_id}", headers=bearer(manager))).status_code == (
        status.HTTP_204_NO_CONTENT
    )

    response = await auth_db_client.post(f"/api/v1/roles/{role_id}/restore", headers=bearer(manager))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None
    assert response.json()["permissions"] == [Permission.EVALUATIONS_READ.value]
    listed = await auth_db_client.get("/api/v1/roles", headers=bearer(manager))
    assert "revivable" in {item["name"] for item in listed.json()["items"]}


async def test_restore_role_keeps_it_deactivated(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    # Activation state is orthogonal to the tombstone: an inactive role comes back
    # inactive, granting nothing until someone reactivates it.
    manager = await caller_with(db_session, Permission.ROLES_MANAGE, Permission.ROLES_READ)
    role = await create_role(db_session, name="dormant", display_name="Dormant", description=None, permissions=[])
    await set_role_active(db_session, role, is_active=False)
    role_id = role.id
    await auth_db_client.delete(f"/api/v1/roles/{role_id}", headers=bearer(manager))

    response = await auth_db_client.post(f"/api/v1/roles/{role_id}/restore", headers=bearer(manager))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["is_active"] is False


async def test_restore_role_409s_when_a_live_role_took_its_name(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # `name` is unique among live rows only, so the delete freed it — a re-created role
    # holds it now and the tombstone cannot come back under it.
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(db_session, name="taken", display_name="Taken", description=None, permissions=[])
    role_id = role.id
    await auth_db_client.delete(f"/api/v1/roles/{role_id}", headers=bearer(manager))
    replacement = await auth_db_client.post(
        "/api/v1/roles",
        json={"name": "taken", "display_name": "Taken Again", "permissions": []},
        headers=bearer(manager),
    )
    assert replacement.status_code == status.HTTP_201_CREATED

    response = await auth_db_client.post(f"/api/v1/roles/{role_id}/restore", headers=bearer(manager))

    assert response.status_code == status.HTTP_409_CONFLICT
    assert "taken" in response.json()["detail"]


async def test_restore_role_404s_outside_the_window(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = await create_role(db_session, name="too-old", display_name="Too Old", description=None, permissions=[])
    role_id = role.id
    await auth_db_client.delete(f"/api/v1/roles/{role_id}", headers=bearer(manager))
    await _backdate_role_tombstone(db_session, role_id, days=30)

    response = await auth_db_client.post(f"/api/v1/roles/{role_id}/restore", headers=bearer(manager))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"] == PROBLEM_CT


async def test_restore_role_rejects_a_system_role(
    auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The delete refuses system roles, so only a direct DB write produces this tombstone —
    # and `sync_system_roles` recreates the name rather than reviving it.
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    role = system_roles[SystemRole.VIEWER.value]
    role.soft_delete(manager.id)
    await db_session.flush()

    response = await auth_db_client.post(f"/api/v1/roles/{role.id}/restore", headers=bearer(manager))

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_restore_role_requires_roles_manage(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE)
    reader = await caller_with(db_session, Permission.ROLES_READ)
    role = await create_role(db_session, name="gated", display_name="Gated", description=None, permissions=[])
    role_id = role.id
    await auth_db_client.delete(f"/api/v1/roles/{role_id}", headers=bearer(manager))

    response = await auth_db_client.post(f"/api/v1/roles/{role_id}/restore", headers=bearer(reader))

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_deleted_listing_requires_roles_manage(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    # `roles:read` opens the catalog; the tombstone list is the restore surface, so it
    # takes the permission that restores.
    reader = await caller_with(db_session, Permission.ROLES_READ)

    response = await auth_db_client.get("/api/v1/roles?deleted=true", headers=bearer(reader))

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"] == PROBLEM_CT


async def test_deleted_listing_shows_every_managers_deletes_newest_first(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # No per-deleter narrowing: `roles:manage` is the only tier, and it restores any
    # tombstone, so hiding another manager's delete would hide a restorable row.
    manager = await caller_with(db_session, Permission.ROLES_MANAGE, Permission.ROLES_READ)
    other = await caller_with(db_session, Permission.ROLES_MANAGE, Permission.ROLES_READ)
    first = await create_role(db_session, name="gone-first", display_name="First", description=None, permissions=[])
    second = await create_role(db_session, name="gone-second", display_name="Second", description=None, permissions=[])
    await auth_db_client.delete(f"/api/v1/roles/{first.id}", headers=bearer(manager))
    await auth_db_client.delete(f"/api/v1/roles/{second.id}", headers=bearer(other))

    listed = await auth_db_client.get("/api/v1/roles?deleted=true", headers=bearer(manager))

    assert listed.status_code == status.HTTP_200_OK
    names = [item["name"] for item in listed.json()["items"]]
    assert names[:2] == ["gone-second", "gone-first"]
    assert {item["deleted_by_id"] for item in listed.json()["items"][:2]} == {str(manager.id), str(other.id)}


async def test_deleted_listing_excludes_tombstones_outside_the_window(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    manager = await caller_with(db_session, Permission.ROLES_MANAGE, Permission.ROLES_READ)
    role = await create_role(db_session, name="ancient", display_name="Ancient", description=None, permissions=[])
    role_id = role.id
    await auth_db_client.delete(f"/api/v1/roles/{role_id}", headers=bearer(manager))
    await _backdate_role_tombstone(db_session, role_id, days=30)

    listed = await auth_db_client.get("/api/v1/roles?deleted=true", headers=bearer(manager))

    assert [item["id"] for item in listed.json()["items"]] == []


async def test_deleted_listing_needs_include_inactive_for_a_deactivated_tombstone(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # The two narrowing flags mean the same thing on both branches; the management view
    # that owns this surface passes `include_inactive=true`, so nothing is unreachable.
    manager = await caller_with(db_session, Permission.ROLES_MANAGE, Permission.ROLES_READ)
    role = await create_role(db_session, name="dormant-gone", display_name="Dormant", description=None, permissions=[])
    await set_role_active(db_session, role, is_active=False)
    role_id = role.id
    await auth_db_client.delete(f"/api/v1/roles/{role_id}", headers=bearer(manager))

    default_view = await auth_db_client.get("/api/v1/roles?deleted=true", headers=bearer(manager))
    full_view = await auth_db_client.get("/api/v1/roles?deleted=true&include_inactive=true", headers=bearer(manager))

    assert [item["id"] for item in default_view.json()["items"]] == []
    assert [item["id"] for item in full_view.json()["items"]] == [str(role_id)]
