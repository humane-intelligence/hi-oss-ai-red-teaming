"""Integration tests for the data-license read endpoints."""

from typing import ClassVar
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.roles import Permission
from app.core.licenses.catalog import CURATED_LICENSES
from app.core.licenses.catalog import NO_LICENSE_SPDX_ID
from app.core.licenses.catalog import curated_license_id
from app.core.licenses.models import DataLicense
from tests.api.v1.conftest import PROBLEM_CT
from tests.api.v1.conftest import caller_with
from tests.api.v1.conftest import make_token


@pytest.mark.integration
class TestLicenseCatalog:
    async def test_any_authenticated_user_may_list(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        # Auth-only — a caller with no permissions still reads the list (it backs the picker).
        caller = await caller_with(db_session, email="noperm@example.com")

        response = await auth_db_client.get(
            "/api/v1/licenses?limit=100", headers={"Authorization": f"Bearer {make_token(caller)}"}
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        # The seeded curated set is the baseline (user-authored licences would add to it).
        assert body["total"] == len(CURATED_LICENSES)
        ids = [item["spdx_id"] for item in body["items"]]
        assert "CC-BY-4.0" in ids
        assert all(item["name"] for item in body["items"])
        # Only the no-license entry ships without a reference url.
        assert [item["spdx_id"] for item in body["items"] if not item["reference_url"]] == [None]
        # `has_content` says whether there is in-app text to read — the list omits the text itself.
        by_name = {item["name"]: item for item in body["items"]}
        assert by_name["No license"]["has_content"] is True
        assert by_name["Creative Commons Attribution 4.0 International"]["has_content"] is False
        assert "content" not in body["items"][0]  # list omits the full body

    async def test_rejects_limit_over_cap(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await caller_with(db_session, email="cap@example.com")

        response = await auth_db_client.get(
            "/api/v1/licenses?limit=101", headers={"Authorization": f"Bearer {make_token(caller)}"}
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert response.json()["errors"]

    async def test_get_one_returns_license_with_content(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await caller_with(db_session, email="one@example.com")

        response = await auth_db_client.get(
            f"/api/v1/licenses/{curated_license_id('CC-BY-4.0')}",
            headers={"Authorization": f"Bearer {make_token(caller)}"},
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["spdx_id"] == "CC-BY-4.0"
        assert body["is_curated"] is True
        assert "content" in body  # detail includes the full body
        assert body["has_content"] is False

    async def test_get_one_serves_a_soft_deleted_license_to_any_caller(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # Evaluations and groups keep resolving a licence after its delete (lineage doesn't lapse),
        # and a user-authored row with no canonical URL has nowhere else to be read — so the detail
        # route serves the tombstone, text included, to the permissionless caller who holds the data.
        author = await caller_with(
            db_session, Permission.LICENSES_CREATE, Permission.LICENSES_DELETE, email="lic-tomb@example.com"
        )
        author_headers = {"Authorization": f"Bearer {make_token(author)}"}
        created = await auth_db_client.post(
            "/api/v1/licenses",
            json={"name": "Gone License 1.0", "short_description": "internal", "content": "GONE TERMS"},
            headers=author_headers,
        )
        license_id = created.json()["id"]
        deleted = await auth_db_client.delete(f"/api/v1/licenses/{license_id}", headers=author_headers)
        assert deleted.status_code == status.HTTP_204_NO_CONTENT
        reader = await caller_with(db_session, email="lic-tomb-reader@example.com")

        response = await auth_db_client.get(
            f"/api/v1/licenses/{license_id}", headers={"Authorization": f"Bearer {make_token(reader)}"}
        )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["content"] == "GONE TERMS"
        assert body["deleted_at"] is not None

    async def test_get_unknown_is_404(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await caller_with(db_session, email="missing@example.com")

        response = await auth_db_client.get(
            f"/api/v1/licenses/{uuid4()}", headers={"Authorization": f"Bearer {make_token(caller)}"}
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.headers["content-type"] == PROBLEM_CT


@pytest.mark.integration
class TestLicenseWrites:
    _EXAMPLE: ClassVar[dict[str, str]] = {
        "name": "Acme Data License 1.0",
        "version": "1.0",
        "short_description": "internal",
        "content": "TEXT",
    }

    async def test_create_returns_201_with_location(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await caller_with(db_session, Permission.LICENSES_CREATE, email="lic-create@example.com")

        response = await auth_db_client.post(
            "/api/v1/licenses", json=self._EXAMPLE, headers={"Authorization": f"Bearer {make_token(caller)}"}
        )

        assert response.status_code == status.HTTP_201_CREATED
        body = response.json()
        assert body["name"] == "Acme Data License 1.0"
        assert body["is_curated"] is False
        assert body["created_by_id"] == str(caller.id)
        assert response.headers["location"] == f"/api/v1/licenses/{body['id']}"

    @pytest.mark.parametrize("method", ["POST", "PATCH"])
    async def test_write_rejects_non_http_reference_url(
        self, method: str, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await caller_with(
            db_session,
            Permission.LICENSES_CREATE,
            Permission.LICENSES_UPDATE,
            email=f"lic-xss-{method.lower()}@example.com",
        )
        headers = {"Authorization": f"Bearer {make_token(caller)}"}
        bad_url = {"reference_url": "javascript:alert(document.cookie)"}

        if method == "POST":
            response = await auth_db_client.post("/api/v1/licenses", json={**self._EXAMPLE, **bad_url}, headers=headers)
        else:
            created = await auth_db_client.post("/api/v1/licenses", json=self._EXAMPLE, headers=headers)
            response = await auth_db_client.patch(
                f"/api/v1/licenses/{created.json()['id']}", json=bad_url, headers=headers
            )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
        assert any("reference_url" in err["loc"] for err in response.json()["errors"])

    async def test_create_forbidden_without_permission(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await caller_with(db_session, email="lic-noperm@example.com")

        response = await auth_db_client.post(
            "/api/v1/licenses", json=self._EXAMPLE, headers={"Authorization": f"Bearer {make_token(caller)}"}
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_update_curated_metadata_forbidden_even_with_manage(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # A curated licence's metadata is managed in code — the API refuses to edit it even for a
        # licenses:manage holder (a resync would revert the change). Its text is the exception.
        caller = await caller_with(
            db_session, Permission.LICENSES_UPDATE, Permission.LICENSES_MANAGE, email="lic-upd@example.com"
        )

        response = await auth_db_client.patch(
            f"/api/v1/licenses/{curated_license_id('CC0-1.0')}",
            json={"name": "hijack"},
            headers={"Authorization": f"Bearer {make_token(caller)}"},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_manager_may_put_text_on_a_curated_license(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await caller_with(
            db_session, Permission.LICENSES_UPDATE, Permission.LICENSES_MANAGE, email="lic-text@example.com"
        )

        response = await auth_db_client.patch(
            f"/api/v1/licenses/{curated_license_id('CC-BY-4.0')}",
            json={"content": "CC BY 4.0 LEGAL CODE"},
            headers={"Authorization": f"Bearer {make_token(caller)}"},
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["content"] == "CC BY 4.0 LEGAL CODE"
        assert response.json()["has_content"] is True

    async def test_patching_the_text_of_a_code_shipped_license_is_forbidden(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # The sentinel carries its own catalog text, so `sync_licenses` rewrites the column on every
        # deploy — the API refuses the edit rather than answering 200 to a write the deploy discards.
        caller = await caller_with(
            db_session, Permission.LICENSES_UPDATE, Permission.LICENSES_MANAGE, email="lic-shipped@example.com"
        )

        response = await auth_db_client.patch(
            f"/api/v1/licenses/{curated_license_id(NO_LICENSE_SPDX_ID)}",
            json={"content": "ours"},
            headers={"Authorization": f"Bearer {make_token(caller)}"},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "shipped in the catalog" in response.json()["detail"]

    async def test_empty_patch_on_curated_is_rejected(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await caller_with(
            db_session, Permission.LICENSES_UPDATE, Permission.LICENSES_MANAGE, email="lic-empty@example.com"
        )

        response = await auth_db_client.patch(
            f"/api/v1/licenses/{curated_license_id('CDLA-Sharing-1.0')}",
            json={},
            headers={"Authorization": f"Bearer {make_token(caller)}"},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    async def test_curated_text_edit_without_manage_is_forbidden(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await caller_with(db_session, Permission.LICENSES_UPDATE, email="lic-text-noman@example.com")

        response = await auth_db_client.patch(
            f"/api/v1/licenses/{curated_license_id('CC-BY-NC-4.0')}",
            json={"content": "sneaky"},
            headers={"Authorization": f"Bearer {make_token(caller)}"},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_delete_curated_forbidden_even_with_manage(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await caller_with(
            db_session, Permission.LICENSES_DELETE, Permission.LICENSES_MANAGE, email="lic-del-cur@example.com"
        )

        response = await auth_db_client.delete(
            f"/api/v1/licenses/{curated_license_id('CC0-1.0')}",
            headers={"Authorization": f"Bearer {make_token(caller)}"},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_delete_platform_default_is_409(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        # The 409 default-guard fires for a user-authored licence set as the default (curated is
        # refused earlier). Create one, make it the default, then try to delete it.
        caller = await caller_with(
            db_session,
            Permission.LICENSES_CREATE,
            Permission.LICENSES_DELETE,
            Permission.PLATFORM_SETTINGS_UPDATE,
            email="lic-del-def@example.com",
        )
        headers = {"Authorization": f"Bearer {make_token(caller)}"}
        created = await auth_db_client.post("/api/v1/licenses", json=self._EXAMPLE, headers=headers)
        license_id = created.json()["id"]
        await auth_db_client.patch(
            "/api/v1/platform-settings", json={"default_license_id": license_id}, headers=headers
        )

        response = await auth_db_client.delete(f"/api/v1/licenses/{license_id}", headers=headers)

        assert response.status_code == status.HTTP_409_CONFLICT

    async def test_soft_deleted_license_stays_unwritable(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # The detail read serves a tombstone; the write paths share its reader and must not. Without
        # this, widening that reader for the one route silently opens the edit and the delete too —
        # the restore endpoint is the only way back.
        caller = await caller_with(
            db_session,
            Permission.LICENSES_CREATE,
            Permission.LICENSES_UPDATE,
            Permission.LICENSES_DELETE,
            email="lic-tomb-write@example.com",
        )
        headers = {"Authorization": f"Bearer {make_token(caller)}"}
        created = await auth_db_client.post("/api/v1/licenses", json=self._EXAMPLE, headers=headers)
        license_id = created.json()["id"]
        deleted = await auth_db_client.delete(f"/api/v1/licenses/{license_id}", headers=headers)
        assert deleted.status_code == status.HTTP_204_NO_CONTENT

        patched = await auth_db_client.patch(
            f"/api/v1/licenses/{license_id}", json={"name": "Revived"}, headers=headers
        )
        redeleted = await auth_db_client.delete(f"/api/v1/licenses/{license_id}", headers=headers)

        assert patched.status_code == status.HTTP_404_NOT_FOUND
        assert redeleted.status_code == status.HTTP_404_NOT_FOUND

    async def test_create_then_delete_own_is_204(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await caller_with(
            db_session, Permission.LICENSES_CREATE, Permission.LICENSES_DELETE, email="lic-crud@example.com"
        )
        headers = {"Authorization": f"Bearer {make_token(caller)}"}

        created = await auth_db_client.post("/api/v1/licenses", json=self._EXAMPLE, headers=headers)
        license_id = created.json()["id"]
        deleted = await auth_db_client.delete(f"/api/v1/licenses/{license_id}", headers=headers)

        assert deleted.status_code == status.HTTP_204_NO_CONTENT

    async def test_create_projects_the_protection_flag(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # Asserted with a non-default value on purpose: the field defaults to False, so `is False`
        # would hold even if the projection dropped the column entirely.
        caller = await caller_with(db_session, Permission.LICENSES_CREATE, email="lic-protect@example.com")

        response = await auth_db_client.post(
            "/api/v1/licenses",
            json={**self._EXAMPLE, "name": "Acme Confidential 1.0", "protects_conversation_data": True},
            headers={"Authorization": f"Bearer {make_token(caller)}"},
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["protects_conversation_data"] is True

    async def test_create_without_the_flag_leaves_conversations_unprotected(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # A client that predates the field must keep working, and must not opt in by accident.
        caller = await caller_with(db_session, Permission.LICENSES_CREATE, email="lic-legacy@example.com")

        response = await auth_db_client.post(
            "/api/v1/licenses", json=self._EXAMPLE, headers={"Authorization": f"Bearer {make_token(caller)}"}
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.json()["protects_conversation_data"] is False

    async def test_patch_toggles_the_protection_flag(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        caller = await caller_with(
            db_session, Permission.LICENSES_CREATE, Permission.LICENSES_UPDATE, email="lic-toggle@example.com"
        )
        headers = {"Authorization": f"Bearer {make_token(caller)}"}
        created = await auth_db_client.post("/api/v1/licenses", json=self._EXAMPLE, headers=headers)

        response = await auth_db_client.patch(
            f"/api/v1/licenses/{created.json()['id']}",
            json={"protects_conversation_data": True},
            headers=headers,
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["protects_conversation_data"] is True

    async def test_patch_cannot_flag_a_curated_license(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # The catalog owns this field on curated rows; accepting the write here would return 200 and be
        # reverted by the next `sync_licenses` run.
        caller = await caller_with(
            db_session, Permission.LICENSES_UPDATE, Permission.LICENSES_MANAGE, email="lic-curated-flag@example.com"
        )

        response = await auth_db_client.patch(
            f"/api/v1/licenses/{curated_license_id('CC0-1.0')}",
            json={"protects_conversation_data": True},
            headers={"Authorization": f"Bearer {make_token(caller)}"},
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_patch_refuses_an_explicit_null_protection_flag(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # The column is NOT NULL and the update applies whatever the payload sets, so an explicit
        # `null` has to be refused at validation — otherwise it reaches the row as an IntegrityError.
        caller = await caller_with(
            db_session, Permission.LICENSES_CREATE, Permission.LICENSES_UPDATE, email="lic-null-flag@example.com"
        )
        headers = {"Authorization": f"Bearer {make_token(caller)}"}
        created = await auth_db_client.post("/api/v1/licenses", json=self._EXAMPLE, headers=headers)

        response = await auth_db_client.patch(
            f"/api/v1/licenses/{created.json()['id']}",
            json={"protects_conversation_data": None},
            headers=headers,
        )

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


class TestLicenseRestore:
    """`POST /v1/licenses/{id}/restore` — gated exactly like an edit of the licence."""

    _EXAMPLE: ClassVar[dict[str, str]] = {
        "name": "Restorable License 1.0",
        "version": "1.0",
        "short_description": "internal",
        "content": "TEXT",
    }

    async def _create_and_delete(self, client: AsyncClient, headers: dict[str, str]) -> str:
        created = await client.post("/api/v1/licenses", json=self._EXAMPLE, headers=headers)
        assert created.status_code == status.HTTP_201_CREATED
        license_id = created.json()["id"]
        deleted = await client.delete(f"/api/v1/licenses/{license_id}", headers=headers)
        assert deleted.status_code == status.HTTP_204_NO_CONTENT
        return license_id

    async def test_author_restores_their_own(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        caller = await caller_with(
            db_session,
            Permission.LICENSES_CREATE,
            Permission.LICENSES_DELETE,
            email="lic-restore-own@example.com",
        )
        headers = {"Authorization": f"Bearer {make_token(caller)}"}
        license_id = await self._create_and_delete(auth_db_client, headers)

        response = await auth_db_client.post(f"/api/v1/licenses/{license_id}/restore", headers=headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["deleted_at"] is None
        listed = await auth_db_client.get("/api/v1/licenses", headers=headers)
        assert license_id in [item["id"] for item in listed.json()["items"]]

    async def test_curated_tombstone_is_forbidden(self, auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
        # Curated rows are reference data owned by `catalog.py`; `synclicenses` revives one.
        # Restoring via the API would fork ownership of a row a deploy step rewrites.
        caller = await caller_with(
            db_session,
            Permission.LICENSES_DELETE,
            Permission.LICENSES_MANAGE,
            email="lic-restore-curated@example.com",
        )
        headers = {"Authorization": f"Bearer {make_token(caller)}"}
        curated_id = curated_license_id("CC0-1.0")
        lic = await db_session.get(DataLicense, curated_id)
        assert lic is not None
        lic.soft_delete(caller.id)
        await db_session.flush()

        response = await auth_db_client.post(f"/api/v1/licenses/{curated_id}/restore", headers=headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN

    async def test_another_authors_licence_needs_manage(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        author = await caller_with(
            db_session,
            Permission.LICENSES_CREATE,
            Permission.LICENSES_DELETE,
            email="lic-restore-author@example.com",
        )
        other = await caller_with(db_session, Permission.LICENSES_DELETE, email="lic-restore-other@example.com")
        license_id = await self._create_and_delete(auth_db_client, {"Authorization": f"Bearer {make_token(author)}"})

        refused = await auth_db_client.post(
            f"/api/v1/licenses/{license_id}/restore",
            headers={"Authorization": f"Bearer {make_token(other)}"},
        )

        assert refused.status_code == status.HTTP_403_FORBIDDEN

    async def test_deleted_listing_follows_the_author_not_the_deleter(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # The one case that separates the two rules: a `licenses:manage` holder deletes
        # someone else's licence, so `created_by_id` and `deleted_by_id` differ. The author
        # is the only caller who may restore it, so the tombstone has to stay in *their*
        # view — scoping on the deleter would hide it from them and show it to the admin.
        author = await caller_with(
            db_session,
            Permission.LICENSES_CREATE,
            Permission.LICENSES_DELETE,
            email="lic-authored@example.com",
        )
        admin = await caller_with(
            db_session,
            Permission.LICENSES_DELETE,
            Permission.LICENSES_MANAGE,
            email="lic-admin-deleter@example.com",
        )
        created = await auth_db_client.post(
            "/api/v1/licenses", json=self._EXAMPLE, headers={"Authorization": f"Bearer {make_token(author)}"}
        )
        assert created.status_code == status.HTTP_201_CREATED
        license_id = created.json()["id"]
        deleted = await auth_db_client.delete(
            f"/api/v1/licenses/{license_id}", headers={"Authorization": f"Bearer {make_token(admin)}"}
        )
        assert deleted.status_code == status.HTTP_204_NO_CONTENT

        mine = await auth_db_client.get(
            "/api/v1/licenses?deleted=true", headers={"Authorization": f"Bearer {make_token(author)}"}
        )

        assert [item["id"] for item in mine.json()["items"]] == [license_id]
        assert mine.json()["items"][0]["deleted_by_id"] == str(admin.id)
        restored = await auth_db_client.post(
            f"/api/v1/licenses/{license_id}/restore", headers={"Authorization": f"Bearer {make_token(author)}"}
        )
        assert restored.status_code == status.HTTP_200_OK

    async def test_deleted_listing_requires_licenses_delete(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # Any authenticated user may read the live picker; the tombstone list is the
        # restore surface, so it needs the delete permission.
        caller = await caller_with(db_session, email="lic-deleted-reader@example.com")

        response = await auth_db_client.get(
            "/api/v1/licenses?deleted=true", headers={"Authorization": f"Bearer {make_token(caller)}"}
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.headers["content-type"].startswith(PROBLEM_CT)

    async def test_deleted_listing_scopes_to_the_callers_own_licences(
        self, auth_db_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        author = await caller_with(
            db_session,
            Permission.LICENSES_CREATE,
            Permission.LICENSES_DELETE,
            email="lic-deleted-mine@example.com",
        )
        other = await caller_with(db_session, Permission.LICENSES_DELETE, email="lic-deleted-theirs@example.com")
        license_id = await self._create_and_delete(auth_db_client, {"Authorization": f"Bearer {make_token(author)}"})

        mine = await auth_db_client.get(
            "/api/v1/licenses?deleted=true", headers={"Authorization": f"Bearer {make_token(author)}"}
        )
        theirs = await auth_db_client.get(
            "/api/v1/licenses?deleted=true", headers={"Authorization": f"Bearer {make_token(other)}"}
        )

        assert [item["id"] for item in mine.json()["items"]] == [license_id]
        assert theirs.json()["items"] == []
