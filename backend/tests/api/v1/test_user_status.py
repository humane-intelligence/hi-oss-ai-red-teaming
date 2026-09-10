"""Integration tests for the account-status endpoints on the `/v1/auth/users` router."""

from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.audit.enums import AuditAction
from app.core.audit.models import AuditLog
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.roles import Permission
from app.core.auth.services import session_revocation
from app.core.auth.services.users import create_user
from tests.api.v1.conftest import bearer
from tests.api.v1.conftest import caller_with
from tests.api.v1.conftest import make_role


async def _clear(user_id: UUID) -> None:
    async with session_revocation._redis() as client:
        await client.delete(session_revocation._key(user_id))


async def _target(db: AsyncSession, *, account_status: UserStatus = UserStatus.ACTIVE) -> User:
    role = await make_role(db)
    return await create_user(
        db,
        email=f"target-{uuid4().hex[:8]}@example.com",
        status=account_status,
        roles=[role],
    )


@pytest_asyncio.fixture
async def admin_role(db_session: AsyncSession) -> Role:
    """Literally named `admin` — `ELEVATED_ROLE_PERMISSIONS` keys the elevation gate on the name."""
    role = Role(name="admin", description="Elevated target", permissions=["users:read"])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest.mark.integration
async def test_deactivate_user_revokes_sessions_and_audits(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session)
    caller_id = caller.id
    target_id = target.id
    target_headers = bearer(target)

    try:
        response = await auth_db_client.post(
            f"/api/v1/auth/users/{target_id}/status",
            json={"status": "inactive"},
            headers=bearer(caller),
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["status"] == "inactive"

        after = await auth_db_client.get("/api/v1/auth/me", headers=target_headers)
        assert after.status_code == status.HTTP_401_UNAUTHORIZED

        db_session.expire_all()
        row = (
            await db_session.execute(
                select(AuditLog).where(
                    col(AuditLog.action) == AuditAction.USER_STATUS_CHANGE.value,
                    col(AuditLog.object_id) == target_id,
                )
            )
        ).scalar_one()
        assert row.actor_id == caller_id
        assert row.before == {"status": "active"}
        assert row.after == {"status": "inactive"}
    finally:
        await _clear(target_id)


@pytest.mark.integration
async def test_activate_user_leaves_sessions_alone(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """Reactivation needs no revocation — the epoch is compared against a fresh token's `iat`."""
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session, account_status=UserStatus.INACTIVE)
    target_headers = bearer(target)

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/status",
        json={"status": "active"},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "active"
    after = await auth_db_client.get("/api/v1/auth/me", headers=target_headers)
    assert after.status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_change_status_rejects_self_target(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, Permission.USERS_UPDATE)

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{caller.id}/status",
        json={"status": "inactive"},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "your own account" in response.json()["detail"]


@pytest.mark.integration
async def test_change_status_forbidden_without_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await caller_with(db_session)
    target = await _target(db_session)

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/status",
        json={"status": "inactive"},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "users:update" in response.json()["detail"]


@pytest.mark.integration
async def test_change_status_blocked_for_admin_target_without_manage_admin(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    """The elevation gate is wired: deactivating an admin needs `users:manage_admin`."""
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await create_user(db_session, email="elevated@example.com", status=UserStatus.ACTIVE, roles=[admin_role])

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/status",
        json={"status": "inactive"},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "admin" in response.json()["detail"]


@pytest.mark.integration
async def test_change_status_unknown_user_returns_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, Permission.USERS_UPDATE)

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{uuid4()}/status",
        json={"status": "inactive"},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_change_status_rejects_onboarding_account(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session, account_status=UserStatus.INVITED)

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/status",
        json={"status": "active"},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.integration
async def test_change_status_rejects_unsettable_status(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """`invited` is not in the request enum — the route rejects it before the service sees it."""
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session)

    response = await auth_db_client.post(
        f"/api/v1/auth/users/{target.id}/status",
        json={"status": "invited"},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert any("status" in error["loc"] for error in response.json()["errors"])


@pytest.mark.integration
async def test_bulk_change_status_reports_per_row(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin_role: Role
) -> None:
    """`r5` is the bulk handler's only proof that it threads the elevation gate.

    The single-route test can't show it, and the two 403 rows come from different guards —
    hence the assertions on `detail`, so both rows tripping the self-target check would fail.
    """
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    active = await _target(db_session)
    onboarding = await _target(db_session, account_status=UserStatus.INVITED)
    elevated = await create_user(
        db_session,
        email=f"elevated-{uuid4().hex[:8]}@example.com",
        status=UserStatus.ACTIVE,
        roles=[admin_role],
    )
    active_headers = bearer(active)

    try:
        response = await auth_db_client.post(
            "/api/v1/auth/users/status",
            json={
                "rows": [
                    {"row_key": "r1", "data": {"user_id": str(active.id), "status": "inactive"}},
                    {"row_key": "r2", "data": {"user_id": str(onboarding.id), "status": "inactive"}},
                    {"row_key": "r3", "data": {"user_id": str(uuid4()), "status": "inactive"}},
                    {"row_key": "r4", "data": {"user_id": str(caller.id), "status": "inactive"}},
                    {"row_key": "r5", "data": {"user_id": str(elevated.id), "status": "inactive"}},
                ]
            },
            headers=bearer(caller),
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert (body["total"], body["succeeded"], body["failed"]) == (5, 1, 4)
        by_key = {r["row_key"]: r for r in body["results"]}
        assert by_key["r1"]["status"] == "ok"
        assert by_key["r1"]["data"]["status"] == "inactive"
        assert by_key["r2"]["error"]["status"] == status.HTTP_409_CONFLICT
        assert by_key["r3"]["error"]["status"] == status.HTTP_404_NOT_FOUND
        assert by_key["r4"]["error"]["status"] == status.HTTP_403_FORBIDDEN
        assert "your own account" in by_key["r4"]["error"]["detail"]
        assert by_key["r5"]["error"]["status"] == status.HTTP_403_FORBIDDEN
        assert "admin" in by_key["r5"]["error"]["detail"]

        after = await auth_db_client.get("/api/v1/auth/me", headers=active_headers)
        assert after.status_code == status.HTTP_401_UNAUTHORIZED
    finally:
        await _clear(active.id)


@pytest.mark.integration
async def test_bulk_change_status_dry_run_has_no_side_effects(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await caller_with(db_session, Permission.USERS_UPDATE)
    target = await _target(db_session)
    target_id = target.id
    target_headers = bearer(target)

    try:
        response = await auth_db_client.post(
            "/api/v1/auth/users/status",
            json={
                "dry_run": True,
                "rows": [{"row_key": "r1", "data": {"user_id": str(target_id), "status": "inactive"}}],
            },
            headers=bearer(caller),
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["succeeded"] == 1

        after = await auth_db_client.get("/api/v1/auth/me", headers=target_headers)
        assert after.status_code == status.HTTP_200_OK

        db_session.expire_all()
        audited = (
            (
                await db_session.execute(
                    select(AuditLog).where(
                        col(AuditLog.action) == AuditAction.USER_STATUS_CHANGE.value,
                        col(AuditLog.object_id) == target_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert audited == []
    finally:
        await _clear(target_id)


@pytest.mark.integration
async def test_bulk_change_status_forbidden_without_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await caller_with(db_session)

    response = await auth_db_client.post(
        "/api/v1/auth/users/status",
        json={"rows": [{"row_key": "r1", "data": {"user_id": str(uuid4()), "status": "inactive"}}]},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_bulk_change_status_rejects_duplicate_row_key(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await caller_with(db_session, Permission.USERS_UPDATE)

    response = await auth_db_client.post(
        "/api/v1/auth/users/status",
        json={
            "rows": [
                {"row_key": "dup", "data": {"user_id": str(uuid4()), "status": "inactive"}},
                {"row_key": "dup", "data": {"user_id": str(uuid4()), "status": "active"}},
            ]
        },
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
