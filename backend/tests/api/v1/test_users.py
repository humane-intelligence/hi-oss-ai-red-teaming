"""Integration tests for the `/v1/auth/users` router."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import Invitation
from app.core.auth.models import InvitationStatus
from app.core.auth.models import ProviderIdentity
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.services.users import create_user as create_user_service
from app.core.auth.services.users import soft_delete_user as soft_delete_user_service
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group


@pytest_asyncio.fixture
async def default_role(db_session: AsyncSession) -> Role:
    role = Role(name="member", description="Default member role", permissions=["users:read"])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def auditor_role(db_session: AsyncSession) -> Role:
    role = Role(name="auditor", description="Read-only auditor", permissions=["users:read"])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def admin_role(db_session: AsyncSession) -> Role:
    role = Role(
        name="admin",
        description="Admin role for test callers performing mutating operations",
        permissions=[
            "users:read",
            "users:update",
            "users:delete",
            "users:invite",
            "users:manage_admin",
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def inviter_role(db_session: AsyncSession) -> Role:
    """Mirrors the real `owner`: holds `users:invite` and none of the elevation permissions."""
    role = Role(name="inviter", description="Invite-only, no admin elevation", permissions=["users:invite"])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def manager_role(db_session: AsyncSession) -> Role:
    """Can update users but lacks `users:manage_admin` — cannot assign the admin role."""
    role = Role(
        name="manager",
        description="User manager without admin-elevation",
        permissions=["users:read", "users:update"],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest.mark.integration
async def test_list_users_returns_page(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="lister@example.com", roles=[default_role])
    await create_user_service(db_session, email="other-1@example.com", roles=[default_role])
    await create_user_service(db_session, email="other-2@example.com", roles=[default_role])

    response = await auth_db_client.get(
        "/api/v1/auth/users",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 3
    assert {item["email"] for item in body["items"]} == {
        "lister@example.com",
        "other-1@example.com",
        "other-2@example.com",
    }
    for item in body["items"]:
        assert [r["id"] for r in item["roles"]] == [str(default_role.id)]


@pytest.mark.integration
async def test_get_user_returns_target(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="caller@example.com", roles=[default_role])
    target = await create_user_service(
        db_session,
        email="target@example.com",
        email_verified=True,
        status=UserStatus.ACTIVE,
        roles=[default_role],
    )

    response = await auth_db_client.get(
        f"/api/v1/auth/users/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["id"] == str(target.id)
    assert body["email"] == "target@example.com"
    assert body["email_verified"] is True
    assert body["status"] == "active"
    assert [r["id"] for r in body["roles"]] == [str(default_role.id)]
    assert "password" not in body
    assert "email_verified_at" not in body
    assert body["invitation"] is None


@pytest.mark.integration
async def test_get_user_embeds_newest_invitation(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="caller@example.com", roles=[default_role])
    target = await create_user_service(
        db_session,
        email="invited@example.com",
        status=UserStatus.INVITED,
        roles=[default_role],
    )
    stale = Invitation(
        user_id=target.id,
        token_hash="1" * 64,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        status=InvitationStatus.REVOKED,
        revoked_at=datetime.now(UTC) - timedelta(hours=2),
    )
    fresh_expiry = datetime.now(UTC) + timedelta(hours=24)
    db_session.add(stale)
    await db_session.flush()
    db_session.add(
        Invitation(
            user_id=target.id,
            token_hash="2" * 64,
            expires_at=fresh_expiry,
            status=InvitationStatus.PENDING,
        )
    )
    await db_session.flush()

    response = await auth_db_client.get(
        f"/api/v1/auth/users/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    invitation = response.json()["invitation"]
    assert invitation["status"] == "pending"
    assert datetime.fromisoformat(invitation["expires_at"]) == fresh_expiry


@pytest.mark.integration
async def test_patch_user_updates_fields(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="patcher@example.com", roles=[admin_role])
    target = await create_user_service(
        db_session,
        email="rename@example.com",
        first_name="Original",
        last_name="Surname",
        roles=[default_role],
    )

    response = await auth_db_client.patch(
        f"/api/v1/auth/users/{target.id}",
        json={"first_name": "Renamed"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["first_name"] == "Renamed"
    assert body["last_name"] == "Surname"
    assert [r["id"] for r in body["roles"]] == [str(default_role.id)]


@pytest.mark.integration
async def test_patch_user_replaces_roles(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
    auditor_role: Role,
    admin_role: Role,
) -> None:
    # Wiring representative for role_ids on PATCH: the route must thread the payload
    # through resolve_assignable_roles into update_user — the service twins can't see that.
    caller = await create_user_service(db_session, email="role-patcher@example.com", roles=[admin_role])
    target = await create_user_service(db_session, email="role-swap@example.com", roles=[default_role])

    response = await auth_db_client.patch(
        f"/api/v1/auth/users/{target.id}",
        json={"role_ids": [str(auditor_role.id)]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert [r["id"] for r in response.json()["roles"]] == [str(auditor_role.id)]


@pytest.mark.integration
async def test_patch_user_assigning_admin_requires_manage_admin(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
    admin_role: Role,
    manager_role: Role,
) -> None:
    """A caller with `users:update` but not `users:manage_admin` can't elevate a user to admin."""
    caller = await create_user_service(db_session, email="mgr@example.com", roles=[manager_role])
    target = await create_user_service(db_session, email="target@example.com", roles=[default_role])

    response = await auth_db_client.patch(
        f"/api/v1/auth/users/{target.id}",
        json={"role_ids": [str(admin_role.id)]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "cannot assign role(s): admin" in response.json()["detail"]


@pytest.mark.integration
async def test_patch_user_removing_admin_requires_manage_admin(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
    admin_role: Role,
    manager_role: Role,
) -> None:
    """A caller with `users:update` but not `users:manage_admin` can't demote an admin.

    Guards the route passing `current_roles=user.roles` into `resolve_assignable_roles` —
    with `current_roles=[]` the demote direction silently stops being gated, which no
    service test can observe.
    """
    caller = await create_user_service(db_session, email="demoter@example.com", roles=[manager_role])
    target = await create_user_service(db_session, email="an-admin@example.com", roles=[admin_role])

    response = await auth_db_client.patch(
        f"/api/v1/auth/users/{target.id}",
        json={"role_ids": [str(default_role.id)]},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "cannot revoke role(s): admin" in response.json()["detail"]


@pytest.mark.integration
async def test_patch_user_rejects_empty_roles(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="empty-patcher@example.com", roles=[admin_role])
    target = await create_user_service(db_session, email="empty-target@example.com", roles=[default_role])

    response = await auth_db_client.patch(
        f"/api/v1/auth/users/{target.id}",
        json={"role_ids": []},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_delete_user_soft_deletes(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="deleter@example.com", roles=[admin_role])
    target = await create_user_service(db_session, email="goodbye@example.com", roles=[default_role])

    response = await auth_db_client.delete(
        f"/api/v1/auth/users/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT

    follow_up = await auth_db_client.get(
        f"/api/v1/auth/users/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_delete_user_blocked_when_sole_group_owner(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role, system_roles: dict[str, Role]
) -> None:
    # Deleting a user who is the sole in-group `owner` would orphan the group
    # (no live owner, manageable only via break-glass) — the user-delete path
    # blocks it with 409, mirroring the member-removal guard.
    caller = await create_user_service(db_session, email="deleter@example.com", roles=[system_roles["admin"]])
    target = await create_user_service(db_session, email="sole-owner@example.com", roles=[default_role])
    await persist_evaluation_group(db_session, created_by_id=target.id)

    response = await auth_db_client.delete(
        f"/api/v1/auth/users/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert "sole owner" in response.json()["detail"]


@pytest.mark.integration
async def test_delete_user_allowed_when_group_has_co_owner(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role, system_roles: dict[str, Role]
) -> None:
    caller = await create_user_service(db_session, email="deleter@example.com", roles=[system_roles["admin"]])
    target = await create_user_service(db_session, email="co-owner@example.com", roles=[default_role])
    group = await persist_evaluation_group(db_session, created_by_id=target.id)
    co_owner = await create_user_service(db_session, email="second-owner@example.com", roles=[default_role])
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, group.id, co_owner.id, [system_roles["owner"]])

    response = await auth_db_client.delete(
        f"/api/v1/auth/users/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.integration
async def test_delete_admin_user_requires_manage_admin(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    admin_role: Role,
) -> None:
    """A caller holding `users:delete` but not `users:manage_admin` can't delete an admin."""
    deleter_role = Role(
        name="user-deleter",
        description="Can delete non-admin users",
        permissions=["users:read", "users:delete"],
    )
    db_session.add(deleter_role)
    await db_session.flush()
    caller = await create_user_service(db_session, email="deleter@example.com", roles=[deleter_role])
    target = await create_user_service(db_session, email="another-admin@example.com", roles=[admin_role])

    response = await auth_db_client.delete(
        f"/api/v1/auth/users/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "cannot delete a user holding role(s): admin" in response.json()["detail"]


@pytest.mark.integration
async def test_delete_user_rejects_self_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="self-delete@example.com", roles=[admin_role])

    response = await auth_db_client.delete(
        f"/api/v1/auth/users/{caller.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"].startswith("application/problem+json")


async def _backdate_user_tombstone(db_session: AsyncSession, user_id: UUID, *, days: int) -> None:
    """Age a tombstone past the restore window.

    Plain `select` (it must see deleted rows) with `populate_existing` to refresh just this
    row — a session-wide `expire_all()` would leave every `User` the test still holds
    expired, and the next attribute read would try a sync lazy-load off the async session.
    """
    statement = select(User).where(col(User.id) == user_id).execution_options(populate_existing=True)
    row = (await db_session.execute(statement)).scalar_one()
    row.deleted_at = datetime.now(UTC) - timedelta(days=days)
    db_session.add(row)
    await db_session.flush()


@pytest.mark.integration
async def test_restore_user_returns_the_account(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="restorer@example.com", roles=[admin_role])
    target = await create_user_service(
        db_session, email="restore-me@example.com", status=UserStatus.ACTIVE, roles=[default_role]
    )
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    assert (
        await auth_db_client.delete(f"/api/v1/auth/users/{target.id}", headers=headers)
    ).status_code == status.HTTP_204_NO_CONTENT

    response = await auth_db_client.post(f"/api/v1/auth/users/{target.id}/restore", headers=headers)

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["deleted_at"] is None
    # Roles and status come back as the delete found them.
    assert [r["id"] for r in body["roles"]] == [str(default_role.id)]
    assert body["status"] == UserStatus.ACTIVE.value
    fetched = await auth_db_client.get(f"/api/v1/auth/users/{target.id}", headers=headers)
    assert fetched.status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_restore_admin_user_requires_manage_admin(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    # The mirror of `test_delete_admin_user_requires_manage_admin`: restoring hands every
    # role on the tombstone back at once, so a caller who could not delete an admin must
    # not be able to mint one by restoring instead.
    deleter_role = Role(
        name="user-deleter",
        description="Can delete non-admin users",
        permissions=["users:read", "users:delete"],
    )
    db_session.add(deleter_role)
    await db_session.flush()
    admin = await create_user_service(db_session, email="admin-caller@example.com", roles=[admin_role])
    caller = await create_user_service(db_session, email="restorer-no-elevation@example.com", roles=[deleter_role])
    target = await create_user_service(db_session, email="deleted-admin@example.com", roles=[admin_role])
    await auth_db_client.delete(f"/api/v1/auth/users/{target.id}", headers={"Authorization": f"Bearer {_token(admin)}"})

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/restore", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "cannot restore a user holding role(s): admin" in response.json()["detail"]


@pytest.mark.integration
async def test_restore_user_leaves_provider_identities_tombstoned(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, default_role: Role
) -> None:
    # Shallow on purpose: the identity's unique index is partial, so the next external
    # login re-links the same `(provider, subject)` as a fresh row.
    caller = await create_user_service(db_session, email="identity-restorer@example.com", roles=[admin_role])
    target = await create_user_service(db_session, email="oidc-user@example.com", roles=[default_role])
    identity = ProviderIdentity(provider="google", subject="sub-restore-1", user_id=target.id)
    db_session.add(identity)
    await db_session.flush()
    identity_id, target_id = identity.id, target.id
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    await auth_db_client.delete(f"/api/v1/auth/users/{target_id}", headers=headers)

    assert (
        await auth_db_client.post(f"/api/v1/auth/users/{target_id}/restore", headers=headers)
    ).status_code == status.HTTP_200_OK

    db_session.expire_all()
    revived = (
        await db_session.execute(select(ProviderIdentity).where(col(ProviderIdentity.id) == identity_id))
    ).scalar_one()
    assert revived.deleted_at is not None


@pytest.mark.integration
async def test_restore_user_409s_when_the_email_was_reused(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, default_role: Role
) -> None:
    # `email` is unique among live rows only, so the delete freed it for a new account.
    caller = await create_user_service(db_session, email="email-restorer@example.com", roles=[admin_role])
    target = await create_user_service(db_session, email="recycled@example.com", roles=[default_role])
    target_id = target.id
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    await auth_db_client.delete(f"/api/v1/auth/users/{target_id}", headers=headers)
    await create_user_service(db_session, email="recycled@example.com", roles=[default_role])

    response = await auth_db_client.post(f"/api/v1/auth/users/{target_id}/restore", headers=headers)

    assert response.status_code == status.HTTP_409_CONFLICT
    assert "recycled@example.com" in response.json()["detail"]


@pytest.mark.integration
async def test_restore_user_409s_when_no_role_is_live(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    # The sole-role guard on role delete skips tombstoned holders, so deleting the account
    # and then its only role is allowed — the restore is where "every live user holds at
    # least one role" gets re-established.
    manager_role = Role(
        name="user-and-role-manager",
        description="users + roles management",
        permissions=["users:read", "users:delete", "roles:read", "roles:manage"],
    )
    db_session.add(manager_role)
    await db_session.flush()
    caller = await create_user_service(db_session, email="roleless-restorer@example.com", roles=[manager_role])
    custom_role = Role(name="sole-role", description="only role", permissions=["users:read"])
    db_session.add(custom_role)
    await db_session.flush()
    target = await create_user_service(db_session, email="roleless@example.com", roles=[custom_role])
    target_id, role_id = target.id, custom_role.id
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    await auth_db_client.delete(f"/api/v1/auth/users/{target_id}", headers=headers)
    assert (
        await auth_db_client.delete(f"/api/v1/roles/{role_id}", headers=headers)
    ).status_code == status.HTTP_204_NO_CONTENT

    response = await auth_db_client.post(f"/api/v1/auth/users/{target_id}/restore", headers=headers)

    assert response.status_code == status.HTTP_409_CONFLICT
    assert "none of its roles is live and active" in response.json()["detail"]
    # Restoring the role first re-opens the account's restore.
    assert (
        await auth_db_client.post(f"/api/v1/roles/{role_id}/restore", headers=headers)
    ).status_code == status.HTTP_200_OK
    assert (
        await auth_db_client.post(f"/api/v1/auth/users/{target_id}/restore", headers=headers)
    ).status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_restore_user_409s_when_no_role_is_active(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    # The deactivate arm of the same hole: the sole-role guard skips tombstoned holders,
    # so the account's only role can be switched off while it is deleted — restoring would
    # otherwise revive a live account with zero effective permissions.
    manager_role = Role(
        name="user-and-role-deactivator",
        description="users + roles management",
        permissions=["users:read", "users:delete", "roles:read", "roles:manage"],
    )
    db_session.add(manager_role)
    await db_session.flush()
    caller = await create_user_service(db_session, email="inactive-restorer@example.com", roles=[manager_role])
    custom_role = Role(name="sole-inactive-role", description="only role", permissions=["users:read"])
    db_session.add(custom_role)
    await db_session.flush()
    target = await create_user_service(db_session, email="inactive-roleless@example.com", roles=[custom_role])
    target_id, role_id = target.id, custom_role.id
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    await auth_db_client.delete(f"/api/v1/auth/users/{target_id}", headers=headers)
    assert (
        await auth_db_client.patch(f"/api/v1/roles/{role_id}", json={"is_active": False}, headers=headers)
    ).status_code == status.HTTP_200_OK

    response = await auth_db_client.post(f"/api/v1/auth/users/{target_id}/restore", headers=headers)

    assert response.status_code == status.HTTP_409_CONFLICT
    assert "none of its roles is live and active" in response.json()["detail"]
    # Reactivating re-opens the account's restore.
    assert (
        await auth_db_client.patch(f"/api/v1/roles/{role_id}", json={"is_active": True}, headers=headers)
    ).status_code == status.HTTP_200_OK
    assert (
        await auth_db_client.post(f"/api/v1/auth/users/{target_id}/restore", headers=headers)
    ).status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_restore_user_404s_outside_the_window(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="stale-restorer@example.com", roles=[admin_role])
    target = await create_user_service(db_session, email="too-old@example.com", roles=[default_role])
    target_id = target.id
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    await auth_db_client.delete(f"/api/v1/auth/users/{target_id}", headers=headers)
    await _backdate_user_tombstone(db_session, target_id, days=30)

    response = await auth_db_client.post(f"/api/v1/auth/users/{target_id}/restore", headers=headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_deleted_listing_requires_users_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role
) -> None:
    # `users:read` opens the directory; the tombstone list is the restore surface.
    caller = await create_user_service(db_session, email="reader-only@example.com", roles=[default_role])

    response = await auth_db_client.get(
        "/api/v1/auth/users?deleted=true", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_deleted_listing_shows_every_deleters_tombstones(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, default_role: Role
) -> None:
    # No per-deleter narrowing: the elevation guard, not the deleter's identity, decides
    # what a caller may restore — so listing another admin's delete is not a dead end.
    first_admin = await create_user_service(db_session, email="admin-one@example.com", roles=[admin_role])
    second_admin = await create_user_service(db_session, email="admin-two@example.com", roles=[admin_role])
    left = await create_user_service(db_session, email="gone-left@example.com", roles=[default_role])
    right = await create_user_service(db_session, email="gone-right@example.com", roles=[default_role])
    await auth_db_client.delete(
        f"/api/v1/auth/users/{left.id}", headers={"Authorization": f"Bearer {_token(first_admin)}"}
    )
    await auth_db_client.delete(
        f"/api/v1/auth/users/{right.id}", headers={"Authorization": f"Bearer {_token(second_admin)}"}
    )

    listed = await auth_db_client.get(
        "/api/v1/auth/users?deleted=true&order_by=-deleted_at",
        headers={"Authorization": f"Bearer {_token(first_admin)}"},
    )

    assert listed.status_code == status.HTTP_200_OK
    items = listed.json()["items"]
    assert [item["email"] for item in items] == ["gone-right@example.com", "gone-left@example.com"]
    assert {item["deleted_by_id"] for item in items} == {str(first_admin.id), str(second_admin.id)}
    live = await auth_db_client.get("/api/v1/auth/users", headers={"Authorization": f"Bearer {_token(first_admin)}"})
    assert {item["email"] for item in live.json()["items"]}.isdisjoint(
        {"gone-left@example.com", "gone-right@example.com"}
    )


@pytest.mark.integration
async def test_deleted_listing_excludes_tombstones_outside_the_window(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="window-lister@example.com", roles=[admin_role])
    target = await create_user_service(db_session, email="aged-out@example.com", roles=[default_role])
    target_id = target.id
    headers = {"Authorization": f"Bearer {_token(caller)}"}
    await auth_db_client.delete(f"/api/v1/auth/users/{target_id}", headers=headers)
    await _backdate_user_tombstone(db_session, target_id, days=30)

    listed = await auth_db_client.get("/api/v1/auth/users?deleted=true", headers=headers)

    assert str(target_id) not in {item["id"] for item in listed.json()["items"]}


@pytest.mark.integration
async def test_list_users_filters_by_status(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    caller = await create_user_service(db_session, email="status-caller@example.com", roles=[default_role])
    await create_user_service(
        db_session, email="active-target@example.com", status=UserStatus.ACTIVE, roles=[default_role]
    )
    await create_user_service(
        db_session, email="invited-target@example.com", status=UserStatus.INVITED, roles=[default_role]
    )

    response = await auth_db_client.get(
        "/api/v1/auth/users",
        params={"status": "active"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "active-target@example.com"


@pytest.mark.integration
async def test_list_users_filters_by_name_substring(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    caller = await create_user_service(db_session, email="name-caller@example.com", roles=[default_role])
    await create_user_service(
        db_session, email="ada@example.com", first_name="Ada", last_name="Lovelace", roles=[default_role]
    )
    await create_user_service(
        db_session, email="grace@example.com", first_name="Grace", last_name="Hopper", roles=[default_role]
    )

    response = await auth_db_client.get(
        "/api/v1/auth/users",
        params={"first_name": "AD", "last_name": "love"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "ada@example.com"


@pytest.mark.integration
async def test_list_users_filters_by_role(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
    auditor_role: Role,
) -> None:
    # Wiring representative for ?role_id= — UUID-typed with an IN-subquery binding
    # no sibling filter shares; a dropped schema field would be silently ignored.
    caller = await create_user_service(db_session, email="role-caller@example.com", roles=[default_role])
    await create_user_service(db_session, email="auditor@example.com", roles=[auditor_role])

    response = await auth_db_client.get(
        "/api/v1/auth/users",
        params={"role_id": str(auditor_role.id)},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "auditor@example.com"


@pytest.mark.integration
async def test_list_users_order_by_descending_email(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    caller = await create_user_service(db_session, email="caller-zzz@example.com", roles=[default_role])
    await create_user_service(db_session, email="aaa@example.com", roles=[default_role])
    await create_user_service(db_session, email="mmm@example.com", roles=[default_role])

    response = await auth_db_client.get(
        "/api/v1/auth/users",
        params={"order_by": "-email"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    emails = [item["email"] for item in response.json()["items"]]
    assert emails == sorted(emails, reverse=True)


@pytest.mark.integration
async def test_list_users_rejects_unknown_order_by(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    caller = await create_user_service(db_session, email="bad-order-by@example.com", roles=[default_role])

    response = await auth_db_client.get(
        "/api/v1/auth/users",
        params={"order_by": "password"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.integration
async def test_list_users_rejects_limit_above_max(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="overlimit@example.com", roles=[default_role])

    response = await auth_db_client.get(
        "/api/v1/auth/users",
        params={"limit": 101},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.integration
async def test_list_users_401_returns_problem_envelope(auth_db_client: AsyncClient) -> None:
    response = await auth_db_client.get("/api/v1/auth/users")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.integration
async def test_get_user_404_returns_problem_envelope(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="envelope-seeker@example.com", roles=[default_role])

    response = await auth_db_client.get(
        f"/api/v1/auth/users/{uuid4()}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.integration
async def test_delete_user_returns_404_when_caller_row_is_soft_deleted(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="ghost@example.com", roles=[admin_role])
    token = _token(caller)
    await soft_delete_user_service(db_session, caller, by_id=uuid4())

    response = await auth_db_client.delete(
        f"/api/v1/auth/users/{caller.id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_list_users_forbidden_for_caller_without_users_read(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    noperm_role = Role(name="noperm", description="No permissions", permissions=[])
    db_session.add(noperm_role)
    await db_session.flush()
    caller = await create_user_service(db_session, email="noread@example.com", roles=[noperm_role])

    response = await auth_db_client.get(
        "/api/v1/auth/users",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"].startswith("application/problem+json")
    assert "users:read" in response.json()["detail"]


@pytest.mark.integration
async def test_patch_user_forbidden_for_caller_without_users_update(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="patch-readonly@example.com", roles=[default_role])
    target = await create_user_service(db_session, email="patch-target@example.com", roles=[default_role])

    response = await auth_db_client.patch(
        f"/api/v1/auth/users/{target.id}",
        json={"first_name": "Renamed"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_delete_user_forbidden_for_caller_without_users_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="delete-readonly@example.com", roles=[default_role])
    target = await create_user_service(db_session, email="delete-target@example.com", roles=[default_role])

    response = await auth_db_client.delete(
        f"/api/v1/auth/users/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
@pytest.mark.usefixtures("celery_enqueue_stub")
async def test_resend_invitation_issues_a_fresh_token(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="inviter@example.com", roles=[admin_role])
    target = await create_user_service(
        db_session,
        email="waiting@example.com",
        status=UserStatus.INVITED,
        roles=[default_role],
    )
    stale = Invitation(
        user_id=target.id,
        token_hash="3" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        status=InvitationStatus.PENDING,
    )
    db_session.add(stale)
    await db_session.flush()

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/invitation/resend",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["user_id"] == str(target.id)
    assert body["email"] == "waiting@example.com"
    assert body["status"] == "pending"
    assert body["invited_by_user_id"] == str(caller.id)
    assert body["id"] != str(stale.id)


@pytest.mark.integration
@pytest.mark.usefixtures("celery_enqueue_stub")
async def test_resend_invitation_rejects_an_active_account(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="inviter@example.com", roles=[admin_role])
    target = await create_user_service(
        db_session,
        email="already-here@example.com",
        status=UserStatus.ACTIVE,
        roles=[default_role],
    )

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/invitation/resend",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["status"] == status.HTTP_409_CONFLICT


@pytest.mark.integration
async def test_resend_invitation_forbidden_without_users_invite(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="nosy@example.com", roles=[default_role])
    target = await create_user_service(
        db_session,
        email="waiting@example.com",
        status=UserStatus.INVITED,
        roles=[default_role],
    )

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/invitation/resend",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_revoke_invitation_kills_the_pending_token(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="revoker@example.com", roles=[admin_role])
    target = await create_user_service(
        db_session,
        email="waiting@example.com",
        status=UserStatus.INVITED,
        roles=[default_role],
    )
    pending = Invitation(
        user_id=target.id,
        token_hash="4" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        status=InvitationStatus.PENDING,
    )
    db_session.add(pending)
    await db_session.flush()

    response = await auth_db_client.delete(
        f"/api/v1/auth/users/{target.id}/invitation",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    await db_session.refresh(pending)
    assert pending.status is InvitationStatus.REVOKED
    assert pending.revoked_at is not None


@pytest.mark.integration
async def test_revoke_invitation_404_when_nothing_pending(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role, admin_role: Role
) -> None:
    caller = await create_user_service(db_session, email="revoker@example.com", roles=[admin_role])
    target = await create_user_service(
        db_session,
        email="settled@example.com",
        status=UserStatus.ACTIVE,
        roles=[default_role],
    )

    response = await auth_db_client.delete(
        f"/api/v1/auth/users/{target.id}/invitation",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.integration
async def test_revoke_invitation_forbidden_without_users_invite(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role
) -> None:
    caller = await create_user_service(db_session, email="nosy@example.com", roles=[default_role])
    target = await create_user_service(
        db_session,
        email="waiting@example.com",
        status=UserStatus.INVITED,
        roles=[default_role],
    )

    response = await auth_db_client.delete(
        f"/api/v1/auth/users/{target.id}/invitation",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
@pytest.mark.parametrize("verb", ["resend", "revoke"])
async def test_invitation_mutation_forbidden_against_an_elevated_target(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    inviter_role: Role,
    admin_role: Role,
    celery_enqueue_stub: MagicMock,
    verb: str,
) -> None:
    """`users:invite` alone must not reach a pending admin — the `owner` role holds it."""
    caller = await create_user_service(db_session, email="owner@example.com", roles=[inviter_role])
    target = await create_user_service(
        db_session,
        email="pending-admin@example.com",
        status=UserStatus.INVITED,
        roles=[admin_role],
    )
    db_session.add(
        Invitation(
            user_id=target.id,
            token_hash="7" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status=InvitationStatus.PENDING,
        ),
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {_token(caller)}"}
    if verb == "resend":
        response = await auth_db_client.post(f"/api/v1/auth/users/{target.id}/invitation/resend", headers=headers)
    else:
        response = await auth_db_client.delete(f"/api/v1/auth/users/{target.id}/invitation", headers=headers)

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Caller cannot manage the invitation of a user holding role(s): admin."
    # The enqueue is the one effect the rolled-back transaction cannot take back, so a gate
    # that let the mail out would still leak a live accept link to the invitee.
    celery_enqueue_stub.assert_not_called()


@pytest.mark.integration
async def test_get_user_omits_a_group_scoped_invitation(
    auth_db_client: AsyncClient, db_session: AsyncSession, default_role: Role
) -> None:
    """The projection is platform-scoped: a group-only invitee reads `invitation: null`.

    Guards the relationship's `object_type IS NULL` filter — the whole platform-vs-group
    split rests on it, and the field's description now promises exactly this.
    """
    caller = await create_user_service(db_session, email="reader@example.com", roles=[default_role])
    target = await create_user_service(
        db_session,
        email="group-only@example.com",
        status=UserStatus.INVITED,
        roles=[default_role],
    )
    db_session.add(
        Invitation(
            user_id=target.id,
            token_hash="8" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status=InvitationStatus.PENDING,
            object_type=ObjectType.EVALUATION_GROUP,
            object_id=uuid4(),
        ),
    )
    await db_session.flush()

    response = await auth_db_client.get(
        f"/api/v1/auth/users/{target.id}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["status"] == "invited"
    assert body["invitation"] is None
