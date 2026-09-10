"""Integration tests for the role and permission catalog endpoints."""

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.roles import NON_DELEGABLE_PERMISSIONS
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from tests.api.v1.conftest import make_token


async def _caller_with_permissions(db: AsyncSession, *, email: str, permissions: list[str]) -> User:
    role = Role(name=f"caller-{email}", permissions=permissions, is_system=False)
    db.add(role)
    await db.flush()
    return await create_user_service(db, email=email, roles=[role])


@pytest.mark.integration
class TestRolesCatalog:
    async def test_returns_catalog(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
    ) -> None:
        caller = await create_user_service(db_session, email="admin@example.com", roles=[system_roles["admin"]])

        response = await auth_db_client.get(
            "/api/v1/roles?limit=100", headers={"Authorization": f"Bearer {make_token(caller)}"}
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        names = {item["name"] for item in body["items"]}
        assert {"admin", "owner", "red_teamer", "annotator", "viewer"} <= names
        admin = next(item for item in body["items"] if item["name"] == "admin")
        assert admin["is_system"] is True
        assert admin["is_active"] is True
        assert admin["is_default"] is False
        assert admin["is_participant_default"] is False
        assert admin["is_object_assignable"] is False
        assert "roles:read" in admin["permissions"]
        red_teamer = next(item for item in body["items"] if item["name"] == "red_teamer")
        assert red_teamer["is_default"] is True
        assert red_teamer["is_participant_default"] is True
        assert red_teamer["is_object_assignable"] is True
        assert body["total"] >= 5

    async def test_rejects_limit_over_cap(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller_with_permissions(db_session, email="cap@example.com", permissions=["roles:read"])

        response = await auth_db_client.get(
            "/api/v1/roles?limit=101", headers={"Authorization": f"Bearer {make_token(caller)}"}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["errors"]


@pytest.mark.integration
class TestPermissionsCatalog:
    async def test_returns_full_catalog(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller_with_permissions(db_session, email="cat@example.com", permissions=["roles:read"])

        response = await auth_db_client.get(
            "/api/v1/permissions?limit=100", headers={"Authorization": f"Bearer {make_token(caller)}"}
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] == len(Permission)
        keys = {item["key"] for item in body["items"]}
        assert {"users:read", "roles:read", "evaluations:approve", "reviews:create"} <= keys
        assert all(item["description"] for item in body["items"])

    async def test_marks_non_delegable_permissions(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        # The role builder filters on this instead of hardcoding the policy list client-side.
        caller = await _caller_with_permissions(db_session, email="deleg@example.com", permissions=["roles:read"])

        response = await auth_db_client.get(
            "/api/v1/permissions?limit=100", headers={"Authorization": f"Bearer {make_token(caller)}"}
        )

        assert response.status_code == status.HTTP_200_OK
        by_key = {item["key"]: item["is_delegable"] for item in response.json()["items"]}
        assert {key for key, delegable in by_key.items() if not delegable} == {
            permission.value for permission in NON_DELEGABLE_PERMISSIONS
        }

    async def test_pagination_windows_the_catalog(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller_with_permissions(db_session, email="page@example.com", permissions=["roles:read"])
        headers = {"Authorization": f"Bearer {make_token(caller)}"}

        first = await auth_db_client.get("/api/v1/permissions?limit=5&offset=0", headers=headers)
        second = await auth_db_client.get("/api/v1/permissions?limit=5&offset=5", headers=headers)

        assert first.status_code == status.HTTP_200_OK
        assert len(first.json()["items"]) == 5
        first_keys = {item["key"] for item in first.json()["items"]}
        second_keys = {item["key"] for item in second.json()["items"]}
        assert not (first_keys & second_keys)
