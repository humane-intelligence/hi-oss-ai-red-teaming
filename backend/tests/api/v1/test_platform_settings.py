"""Integration tests for the platform-settings singleton endpoints."""

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.services.users import create_user as create_user_service
from app.core.licenses.catalog import NO_LICENSE_SPDX_ID
from app.core.licenses.catalog import curated_license_id
from app.core.licenses.models import DataLicense
from app.core.platform_settings.models import PLATFORM_SETTINGS_ID
from app.core.platform_settings.models import PlatformSettings
from app.core.platform_settings.service import update_platform_settings
from tests.api.v1.conftest import bearer as _auth

_READ_WRITE = ["platform_settings:read", "platform_settings:update"]


async def _caller(db: AsyncSession, *, email: str, permissions: list[str]) -> User:
    role = Role(name=f"caller-{email}", permissions=permissions, is_system=False)
    db.add(role)
    await db.flush()
    return await create_user_service(db, email=email, roles=[role])


@pytest.mark.integration
class TestGetPlatformSettings:
    async def test_returns_default_before_any_override(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await _caller(db_session, email="reader@example.com", permissions=["platform_settings:read"])

        response = await auth_db_client.get("/api/v1/platform-settings", headers=_auth(caller))

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["default_license_id"] == str(curated_license_id("CC-BY-4.0"))
        assert body["invite_only"] is False
        assert body["email_verification_ttl_hours"] == 24
        assert body["password_min_length"] == 8
        assert body["password_require_uppercase"] is False
        assert body["password_require_digit"] is False
        assert body["password_require_symbol"] is False
        assert body["password_reset_cooldown_seconds"] == 60
        assert body["password_reset_max_per_day"] == 5
        assert body["updated_at"] is None  # transient — never overridden


@pytest.mark.integration
class TestGetPublicPlatformSettings:
    async def test_anonymous_reads_shipped_defaults(self, auth_db_client: AsyncClient) -> None:
        response = await auth_db_client.get("/api/v1/platform-settings/public")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "signup_enabled": True,
            "password_policy": {
                "min_length": 8,
                "require_uppercase": False,
                "require_digit": False,
                "require_symbol": False,
            },
        }
        # Anonymous + fetched per login-screen load — must be shared-cacheable.
        assert response.headers["cache-control"] == "public, max-age=30"

    async def test_signup_disabled_under_invite_only(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await update_platform_settings(db_session, invite_only=True)

        response = await auth_db_client.get("/api/v1/platform-settings/public")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["signup_enabled"] is False

    async def test_password_policy_reflects_the_admin_override(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await update_platform_settings(db_session, password_min_length=16, password_require_symbol=True)

        response = await auth_db_client.get("/api/v1/platform-settings/public")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["password_policy"] == {
            "min_length": 16,
            "require_uppercase": False,
            "require_digit": False,
            "require_symbol": True,
        }

    async def test_throttling_knobs_stay_out_of_the_public_subset(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # Operational, not part of the form contract — a client has no use for them and they
        # describe how much abuse the platform tolerates.
        await update_platform_settings(db_session, password_reset_cooldown_seconds=300)

        response = await auth_db_client.get("/api/v1/platform-settings/public")

        assert response.status_code == status.HTTP_200_OK
        assert response.json().keys() == {"signup_enabled", "password_policy"}


@pytest.mark.integration
class TestUpdatePlatformSettings:
    async def test_update_persists_and_is_readable(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="admin@example.com", permissions=_READ_WRITE)
        cc0 = str(curated_license_id("CC0-1.0"))

        patched = await auth_db_client.patch(
            "/api/v1/platform-settings", json={"default_license_id": cc0}, headers=_auth(caller)
        )
        fetched = await auth_db_client.get("/api/v1/platform-settings", headers=_auth(caller))

        assert patched.status_code == status.HTTP_200_OK
        assert patched.json()["default_license_id"] == cc0
        assert patched.json()["updated_at"] is not None  # row materialized on first write
        assert fetched.json()["default_license_id"] == cc0

    async def test_rejects_unknown_license_id(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        # Well-formed UUID that references no live licence → 400 from the service guard.
        caller = await _caller(db_session, email="bad@example.com", permissions=_READ_WRITE)

        response = await auth_db_client.patch(
            "/api/v1/platform-settings", json={"default_license_id": str(uuid4())}, headers=_auth(caller)
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    async def test_rejects_the_no_license_sentinel(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        # A live curated row, so the licence-ref guard passes it — setting it platform-wide would
        # unlicense everything that inherits.
        caller = await _caller(db_session, email="sentinel@example.com", permissions=_READ_WRITE)

        response = await auth_db_client.patch(
            "/api/v1/platform-settings",
            json={"default_license_id": str(curated_license_id(NO_LICENSE_SPDX_ID))},
            headers=_auth(caller),
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    async def test_rejects_non_uuid(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="notuuid@example.com", permissions=_READ_WRITE)

        response = await auth_db_client.patch(
            "/api/v1/platform-settings", json={"default_license_id": "NOPE"}, headers=_auth(caller)
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert "default_license_id" in response.json()["errors"][0]["loc"]

    async def test_rejects_explicit_null(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await _caller(db_session, email="null@example.com", permissions=_READ_WRITE)

        response = await auth_db_client.patch(
            "/api/v1/platform-settings", json={"default_license_id": None}, headers=_auth(caller)
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_update_invite_only_alone_keeps_license(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await _caller(db_session, email="invite@example.com", permissions=_READ_WRITE)

        patched = await auth_db_client.patch(
            "/api/v1/platform-settings", json={"invite_only": True}, headers=_auth(caller)
        )
        fetched = await auth_db_client.get("/api/v1/platform-settings", headers=_auth(caller))

        assert patched.status_code == status.HTTP_200_OK
        assert patched.json()["invite_only"] is True
        # First-ever write of the other knob still materializes the effective license default.
        assert fetched.json()["default_license_id"] == str(curated_license_id("CC-BY-4.0"))
        assert fetched.json()["invite_only"] is True

    async def test_noop_patch_does_not_materialize_the_row(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # Writing the effective values back is a no-op: the singleton must not silently
        # materialize, and the response's `updated_at` keeps signalling "shipped defaults".
        caller = await _caller(db_session, email="noop@example.com", permissions=_READ_WRITE)

        patched = await auth_db_client.patch(
            "/api/v1/platform-settings", json={"invite_only": False}, headers=_auth(caller)
        )

        assert patched.status_code == status.HTTP_200_OK
        assert await db_session.get(PlatformSettings, PLATFORM_SETTINGS_ID) is None
        assert patched.json()["updated_at"] is None

    async def test_first_write_with_unsynced_catalog_returns_400(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # The first-ever write materializes the shipped-default licence FK too — with the
        # curated catalog missing (`synclicenses` not run) it must refuse legibly, not 500.
        caller = await _caller(db_session, email="unsynced@example.com", permissions=_READ_WRITE)
        curated = await db_session.get(DataLicense, curated_license_id("CC-BY-4.0"))
        assert curated is not None
        curated.deleted_at = datetime.now(UTC)
        await db_session.flush()

        response = await auth_db_client.patch(
            "/api/v1/platform-settings", json={"invite_only": True}, headers=_auth(caller)
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert await db_session.get(PlatformSettings, PLATFORM_SETTINGS_ID) is None

    async def test_rejects_explicit_null_invite_only(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await _caller(db_session, email="nullinvite@example.com", permissions=_READ_WRITE)

        response = await auth_db_client.patch(
            "/api/v1/platform-settings", json={"invite_only": None}, headers=_auth(caller)
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_update_ttl_alone_keeps_other_knobs(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await _caller(db_session, email="ttl@example.com", permissions=_READ_WRITE)

        patched = await auth_db_client.patch(
            "/api/v1/platform-settings", json={"email_verification_ttl_hours": 72}, headers=_auth(caller)
        )

        assert patched.status_code == status.HTTP_200_OK
        body = patched.json()
        assert body["email_verification_ttl_hours"] == 72
        assert body["default_license_id"] == str(curated_license_id("CC-BY-4.0"))
        assert body["invite_only"] is False

    @pytest.mark.parametrize("edge_ttl", [1, 8760])
    async def test_accepts_the_ttl_bounds_themselves(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, edge_ttl: int
    ) -> None:
        # Pins the inclusive edges: without these a `ge=2` / `le=8759` slip passes the
        # rejecting cases below unchanged.
        caller = await _caller(db_session, email=f"edgettl{edge_ttl}@example.com", permissions=_READ_WRITE)

        response = await auth_db_client.patch(
            "/api/v1/platform-settings", json={"email_verification_ttl_hours": edge_ttl}, headers=_auth(caller)
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["email_verification_ttl_hours"] == edge_ttl

    @pytest.mark.parametrize("bad_ttl", [0, -1, 8761, None])
    async def test_rejects_out_of_bounds_or_null_ttl(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, bad_ttl: int | None
    ) -> None:
        caller = await _caller(db_session, email=f"badttl{bad_ttl}@example.com", permissions=_READ_WRITE)

        response = await auth_db_client.patch(
            "/api/v1/platform-settings", json={"email_verification_ttl_hours": bad_ttl}, headers=_auth(caller)
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_update_password_policy_alone_keeps_other_knobs(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await _caller(db_session, email="policy@example.com", permissions=_READ_WRITE)

        patched = await auth_db_client.patch(
            "/api/v1/platform-settings",
            json={"password_min_length": 12, "password_require_symbol": True},
            headers=_auth(caller),
        )

        assert patched.status_code == status.HTTP_200_OK
        body = patched.json()
        assert body["password_min_length"] == 12
        assert body["password_require_symbol"] is True
        assert body["password_require_digit"] is False
        assert body["email_verification_ttl_hours"] == 24
        assert body["default_license_id"] == str(curated_license_id("CC-BY-4.0"))

    @pytest.mark.parametrize(
        ("knob", "edge"),
        [
            ("password_min_length", 8),
            ("password_min_length", 128),
            ("password_reset_cooldown_seconds", 0),
            ("password_reset_cooldown_seconds", 3600),
            ("password_reset_max_per_day", 1),
            ("password_reset_max_per_day", 100),
        ],
    )
    async def test_accepts_the_password_knob_bounds_themselves(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, knob: str, edge: int
    ) -> None:
        caller = await _caller(db_session, email=f"edge{knob}{edge}@example.com", permissions=_READ_WRITE)

        response = await auth_db_client.patch("/api/v1/platform-settings", json={knob: edge}, headers=_auth(caller))

        assert response.status_code == status.HTTP_200_OK
        assert response.json()[knob] == edge

    @pytest.mark.parametrize(
        ("knob", "bad"),
        [
            # The floor is the `min_length` the set-password schemas carry — a lower minimum
            # would promise what those already reject with a 422.
            ("password_min_length", 7),
            ("password_min_length", 129),
            ("password_min_length", None),
            ("password_reset_cooldown_seconds", -1),
            ("password_reset_cooldown_seconds", 3601),
            ("password_reset_max_per_day", 0),
            ("password_reset_max_per_day", 101),
            ("password_require_digit", None),
        ],
    )
    async def test_rejects_out_of_bounds_or_null_password_knobs(
        self, auth_db_client: AsyncClient, db_session: AsyncSession, knob: str, bad: int | None
    ) -> None:
        caller = await _caller(db_session, email=f"bad{knob}{bad}@example.com", permissions=_READ_WRITE)

        response = await auth_db_client.patch("/api/v1/platform-settings", json={knob: bad}, headers=_auth(caller))

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
