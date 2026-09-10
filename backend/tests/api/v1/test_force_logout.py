"""Integration tests for the force-logout endpoints on the `/v1/auth/users` router."""

from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.audit.enums import AuditAction
from app.core.audit.models import AuditLog
from app.core.auth.roles import Permission
from app.core.auth.services import session_revocation
from tests.api.v1.conftest import bearer
from tests.api.v1.conftest import caller_with


async def _clear(user_id: UUID) -> None:
    async with session_revocation._redis() as client:
        await client.delete(session_revocation._key(user_id))


@pytest.mark.integration
async def test_force_logout_revokes_target_sessions(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, Permission.USERS_MANAGE_SESSIONS)
    target = await caller_with(db_session)
    caller_id = caller.id
    target_id = target.id
    target_headers = bearer(target)

    before = await auth_db_client.get("/api/v1/auth/me", headers=target_headers)
    assert before.status_code == status.HTTP_200_OK

    try:
        response = await auth_db_client.post(f"/api/v1/auth/users/{target_id}/force-logout", headers=bearer(caller))
        assert response.status_code == status.HTTP_204_NO_CONTENT

        after = await auth_db_client.get("/api/v1/auth/me", headers=target_headers)
        assert after.status_code == status.HTTP_401_UNAUTHORIZED

        db_session.expire_all()
        row = (
            await db_session.execute(
                select(AuditLog).where(
                    col(AuditLog.action) == AuditAction.USER_FORCE_LOGOUT.value,
                    col(AuditLog.object_id) == target_id,
                )
            )
        ).scalar_one()
        assert row.actor_id == caller_id
        assert row.object_type == "user"
    finally:
        await _clear(target_id)


@pytest.mark.integration
async def test_force_logout_forbidden_without_permission(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session)
    target = await caller_with(db_session)

    response = await auth_db_client.post(f"/api/v1/auth/users/{target.id}/force-logout", headers=bearer(caller))

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert "users:manage_sessions" in response.json()["detail"]


@pytest.mark.integration
async def test_force_logout_unknown_user_returns_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, Permission.USERS_MANAGE_SESSIONS)

    response = await auth_db_client.post(f"/api/v1/auth/users/{uuid4()}/force-logout", headers=bearer(caller))

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_bulk_force_logout_forbidden_without_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await caller_with(db_session)

    response = await auth_db_client.post(
        "/api/v1/auth/users/force-logout",
        json={"rows": [{"row_key": "r1", "data": {"user_id": str(uuid4())}}]},
        headers=bearer(caller),
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_bulk_force_logout_reports_per_row(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, Permission.USERS_MANAGE_SESSIONS)
    t1 = await caller_with(db_session)
    t2 = await caller_with(db_session)
    t1_headers = bearer(t1)

    try:
        response = await auth_db_client.post(
            "/api/v1/auth/users/force-logout",
            json={
                "rows": [
                    {"row_key": "r1", "data": {"user_id": str(t1.id)}},
                    {"row_key": "r2", "data": {"user_id": str(t2.id)}},
                    {"row_key": "r3", "data": {"user_id": str(uuid4())}},
                ]
            },
            headers=bearer(caller),
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["dry_run"] is False
        assert (body["total"], body["succeeded"], body["failed"]) == (3, 2, 1)
        by_key = {r["row_key"]: r for r in body["results"]}
        assert by_key["r1"]["status"] == "ok"
        assert by_key["r3"]["status"] == "failed"
        assert by_key["r3"]["error"] is not None

        after = await auth_db_client.get("/api/v1/auth/me", headers=t1_headers)
        assert after.status_code == status.HTTP_401_UNAUTHORIZED
    finally:
        await _clear(t1.id)
        await _clear(t2.id)


@pytest.mark.integration
async def test_bulk_force_logout_dry_run_revokes_nothing(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, Permission.USERS_MANAGE_SESSIONS)
    target = await caller_with(db_session)
    target_headers = bearer(target)

    try:
        response = await auth_db_client.post(
            "/api/v1/auth/users/force-logout",
            json={"dry_run": True, "rows": [{"row_key": "r1", "data": {"user_id": str(target.id)}}]},
            headers=bearer(caller),
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["dry_run"] is True
        assert body["succeeded"] == 1

        after = await auth_db_client.get("/api/v1/auth/me", headers=target_headers)
        assert after.status_code == status.HTTP_200_OK
    finally:
        await _clear(target.id)
