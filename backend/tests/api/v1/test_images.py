"""Integration tests for the image upload / serve / delete + signed-URL endpoints."""

import io
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
import time_machine
from fastapi import status
from httpx import AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.services.users import create_user as create_user_service
from app.core.config import get_settings
from app.core.media.storage.local import LocalMediaStorage
from app.core.terms.service import TermsAcceptanceRequiredError
from app.core.terms.service import publish_terms
from tests.api.v1.conftest import as_user

pytestmark = pytest.mark.integration


def _png(width: int = 400, height: int = 300) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
    return buffer.getvalue()


async def _make_user(db: AsyncSession, email: str) -> User:
    role = Role(name=f"role-{email}", permissions=[], is_system=False)
    db.add(role)
    await db.flush()
    return await create_user_service(db, email=email, roles=[role])


@pytest.fixture
def media_storage(tmp_path, monkeypatch: pytest.MonkeyPatch) -> LocalMediaStorage:
    """Point both storage lookups (service + endpoint) at a throwaway dir — no real var/media writes."""
    storage = LocalMediaStorage(str(tmp_path))
    monkeypatch.setattr("app.core.media.services.images.get_media_storage", lambda: storage)
    monkeypatch.setattr("app.api.v1.images.get_media_storage", lambda: storage)
    return storage


@pytest.mark.usefixtures("media_storage")
async def test_upload_returns_created_asset(
    async_client_with_db: AsyncClient, db_session: AsyncSession, media_storage: LocalMediaStorage
) -> None:
    user = await _make_user(db_session, "uploader@example.com")
    with as_user(user):
        response = await async_client_with_db.post("/api/v1/images", files={"file": ("cover.png", _png(), "image/png")})

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["content_type"] == "image/png"
    assert (body["width"], body["height"]) == (400, 300)
    assert body["size_bytes"] > 0
    assert body["is_private"] is False
    assert body["url"] == f"/api/v1/images/{body['key']}"
    assert response.headers["Location"] == body["url"]
    assert media_storage.exists(body["key"])


@pytest.mark.usefixtures("media_storage")
async def test_upload_rejects_non_image(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "baduploader@example.com")
    with as_user(user):
        response = await async_client_with_db.post(
            "/api/v1/images", files={"file": ("x.png", b"not an image", "image/png")}
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.usefixtures("media_storage")
async def test_get_image_is_public_and_cacheable(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "getowner@example.com")
    with as_user(user):
        created = await async_client_with_db.post("/api/v1/images", files={"file": ("cover.png", _png(), "image/png")})
    key = created.json()["key"]

    # No auth override / no token here — the GET must serve anonymously.
    response = await async_client_with_db.get(f"/api/v1/images/{key}")

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["content-type"] == "image/png"
    assert "immutable" in response.headers["cache-control"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert Image.open(io.BytesIO(response.content)).format == "PNG"


async def test_get_missing_returns_404(async_client_with_db: AsyncClient) -> None:
    response = await async_client_with_db.get("/api/v1/images/2026/01/01/deadbeef.png")
    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.usefixtures("media_storage")
async def test_delete_by_owner_removes_it(
    async_client_with_db: AsyncClient, db_session: AsyncSession, media_storage: LocalMediaStorage
) -> None:
    user = await _make_user(db_session, "delowner@example.com")
    with as_user(user):
        created = await async_client_with_db.post("/api/v1/images", files={"file": ("cover.png", _png(), "image/png")})
        key = created.json()["key"]
        deleted = await async_client_with_db.delete(f"/api/v1/images/{key}")

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert not media_storage.exists(key)
    assert (await async_client_with_db.get(f"/api/v1/images/{key}")).status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.usefixtures("media_storage")
async def test_delete_tolerates_blob_failure(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    media_storage: LocalMediaStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _make_user(db_session, "blobfail@example.com")
    with as_user(user):
        created = await async_client_with_db.post("/api/v1/images", files={"file": ("cover.png", _png(), "image/png")})
        key = created.json()["key"]

        async def _boom(_key: str) -> None:
            raise OSError("storage backend down")

        monkeypatch.setattr(media_storage, "delete", _boom)
        deleted = await async_client_with_db.delete(f"/api/v1/images/{key}")

    # Soft-delete is authoritative: the row is gone (GET 404s) even though the blob delete raised.
    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert (await async_client_with_db.get(f"/api/v1/images/{key}")).status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.usefixtures("media_storage")
async def test_delete_by_non_owner_forbidden(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _make_user(db_session, "owner2@example.com")
    other = await _make_user(db_session, "intruder@example.com")
    with as_user(owner):
        created = await async_client_with_db.post("/api/v1/images", files={"file": ("cover.png", _png(), "image/png")})
    key = created.json()["key"]

    with as_user(other):
        response = await async_client_with_db.delete(f"/api/v1/images/{key}")

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.usefixtures("media_storage")
async def test_upload_rejects_oversized(
    async_client_with_db: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.v1.images.MAX_UPLOAD_BYTES", 10)  # any real image exceeds this
    user = await _make_user(db_session, "big@example.com")
    with as_user(user):
        response = await async_client_with_db.post("/api/v1/images", files={"file": ("cover.png", _png(), "image/png")})

    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.usefixtures("media_storage")
async def test_delete_missing_returns_404(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "delmiss@example.com")
    with as_user(user):
        response = await async_client_with_db.delete("/api/v1/images/2026/01/01/deadbeef.png")

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.usefixtures("media_storage")
async def test_upload_private_hidden_from_public_get(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _make_user(db_session, "private@example.com")
    with as_user(user):
        created = await async_client_with_db.post(
            "/api/v1/images",
            files={"file": ("secret.png", _png(), "image/png")},
            data={"is_private": "true"},
        )

    assert created.status_code == status.HTTP_201_CREATED
    body = created.json()
    assert body["is_private"] is True
    # The bare key no longer grants access — the public GET reads the asset as absent.
    assert (await async_client_with_db.get(f"/api/v1/images/{body['key']}")).status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.usefixtures("media_storage")
async def test_private_asset_fetches_via_signed_url(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _make_user(db_session, "privatesigned@example.com")
    with as_user(user):
        created = await async_client_with_db.post(
            "/api/v1/images",
            files={"file": ("secret.png", _png(), "image/png")},
            data={"is_private": "true"},
        )
        minted = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": created.json()["key"]})
        fetched = await async_client_with_db.get(minted.json()["url"])

    assert minted.status_code == status.HTTP_200_OK
    assert fetched.status_code == status.HTTP_200_OK
    assert fetched.headers["content-type"] == "image/png"


@pytest.mark.usefixtures("media_storage")
async def test_private_mint_requires_auth(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "privmintanon@example.com")
    with as_user(user):
        created = await async_client_with_db.post(
            "/api/v1/images",
            files={"file": ("secret.png", _png(), "image/png")},
            data={"is_private": "true"},
        )

    response = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": created.json()["key"]})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.usefixtures("media_storage")
async def test_private_signed_fetch_rejects_anonymous(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _make_user(db_session, "privanon@example.com")
    with as_user(owner):
        created = await async_client_with_db.post(
            "/api/v1/images",
            files={"file": ("secret.png", _png(), "image/png")},
            data={"is_private": "true"},
        )
        minted = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": created.json()["key"]})

    response = await async_client_with_db.get(minted.json()["url"])

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.usefixtures("media_storage")
async def test_private_mint_is_refused_to_a_caller_owing_a_terms_acceptance(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # The private branch binds the token to the caller, so identity authorises it — one of the two
    # optional-auth cases the route-level gate does not cover.
    user = await _make_user(db_session, "privmintterms@example.com")
    with as_user(user):
        created = await async_client_with_db.post(
            "/api/v1/images",
            files={"file": ("secret.png", _png(), "image/png")},
            data={"is_private": "true"},
        )
        await publish_terms(db_session, version="1.0", content="Terms")
        response = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": created.json()["key"]})

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["type"] == TermsAcceptanceRequiredError.type


@pytest.mark.usefixtures("media_storage")
async def test_private_signed_fetch_is_refused_to_a_caller_owing_a_terms_acceptance(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _make_user(db_session, "privfetchterms@example.com")
    with as_user(user):
        created = await async_client_with_db.post(
            "/api/v1/images",
            files={"file": ("secret.png", _png(), "image/png")},
            data={"is_private": "true"},
        )
        minted = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": created.json()["key"]})
        await publish_terms(db_session, version="1.0", content="Terms")
        response = await async_client_with_db.get(minted.json()["url"])

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["type"] == TermsAcceptanceRequiredError.type


@pytest.mark.usefixtures("media_storage")
async def test_public_mint_still_answers_a_caller_owing_a_terms_acceptance(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # A public image needs no identity, so the gate must not reach it: the key is the capability.
    user = await _make_user(db_session, "pubmintterms@example.com")
    with as_user(user):
        created = await async_client_with_db.post("/api/v1/images", files={"file": ("open.png", _png(), "image/png")})
        await publish_terms(db_session, version="1.0", content="Terms")
        response = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": created.json()["key"]})

    assert response.status_code == status.HTTP_200_OK


@pytest.mark.usefixtures("media_storage")
async def test_public_signed_fetch_still_answers_a_caller_owing_a_terms_acceptance(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    # An unbound token carries no identity to authorise, so the gate must not reach this branch
    # even when the caller happens to be authenticated and owes an acceptance.
    user = await _make_user(db_session, "pubfetchterms@example.com")
    with as_user(user):
        created = await async_client_with_db.post("/api/v1/images", files={"file": ("open.png", _png(), "image/png")})
        minted = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": created.json()["key"]})
        await publish_terms(db_session, version="1.0", content="Terms")
        response = await async_client_with_db.get(minted.json()["url"])

    assert response.status_code == status.HTTP_200_OK


@pytest.mark.usefixtures("media_storage")
async def test_private_signed_fetch_rejects_other_user(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _make_user(db_session, "privowner@example.com")
    other = await _make_user(db_session, "privother@example.com")
    with as_user(owner):
        created = await async_client_with_db.post(
            "/api/v1/images",
            files={"file": ("secret.png", _png(), "image/png")},
            data={"is_private": "true"},
        )
        minted = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": created.json()["key"]})

    with as_user(other):
        response = await async_client_with_db.get(minted.json()["url"])

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.usefixtures("media_storage")
async def test_signed_url_mints_and_fetches(async_client_with_db: AsyncClient, db_session: AsyncSession) -> None:
    user = await _make_user(db_session, "signowner@example.com")
    with as_user(user):
        created = await async_client_with_db.post("/api/v1/images", files={"file": ("cover.png", _png(), "image/png")})
    key = created.json()["key"]

    # Mint with no auth — the signed URL is public, like the plain GET.
    minted = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": key})

    assert minted.status_code == status.HTTP_200_OK
    body = minted.json()
    assert body["url"].startswith("/api/v1/images/signed/")
    assert "expires_at" in body

    fetched = await async_client_with_db.get(body["url"])

    assert fetched.status_code == status.HTTP_200_OK
    assert fetched.headers["content-type"] == "image/png"
    assert "private" in fetched.headers["cache-control"]
    assert "immutable" not in fetched.headers["cache-control"]
    assert fetched.headers["x-content-type-options"] == "nosniff"
    assert Image.open(io.BytesIO(fetched.content)).format == "PNG"


async def test_signed_url_mint_missing_returns_404(async_client_with_db: AsyncClient) -> None:
    response = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": "2026/01/01/deadbeef.png"})
    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_signed_url_route_not_shadowed_by_key(async_client_with_db: AsyncClient) -> None:
    # With no ?key this must resolve to the mint route (422 missing param), not the /{key:path} GET (404).
    response = await async_client_with_db.get("/api/v1/images/signed-url")
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.usefixtures("media_storage")
async def test_signed_fetch_tampered_token_forbidden(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _make_user(db_session, "signtamper@example.com")
    with as_user(user):
        created = await async_client_with_db.post("/api/v1/images", files={"file": ("cover.png", _png(), "image/png")})
    minted = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": created.json()["key"]})
    token = minted.json()["url"].rsplit("/", 1)[1]

    response = await async_client_with_db.get(f"/api/v1/images/signed/{token}tampered")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"] == "application/problem+json"


async def test_signed_fetch_garbage_token_not_shadowed_by_key(async_client_with_db: AsyncClient) -> None:
    # Must resolve to the /signed/{token} route (403 invalid token), not fall through to /{key:path} (404).
    response = await async_client_with_db.get("/api/v1/images/signed/garbage")

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.usefixtures("media_storage")
async def test_signed_fetch_expired_token_forbidden(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _make_user(db_session, "signexp@example.com")
    with as_user(user):
        created = await async_client_with_db.post("/api/v1/images", files={"file": ("cover.png", _png(), "image/png")})
    minted = await async_client_with_db.get("/api/v1/images/signed-url", params={"key": created.json()["key"]})
    signed_url = minted.json()["url"]

    ttl = get_settings().media_signed_url_ttl_seconds
    with time_machine.travel(datetime.now(UTC) + timedelta(seconds=ttl + 5)):
        response = await async_client_with_db.get(signed_url)

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.usefixtures("media_storage")
async def test_signed_fetch_deleted_asset_returns_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _make_user(db_session, "signdel@example.com")
    with as_user(user):
        created = await async_client_with_db.post("/api/v1/images", files={"file": ("cover.png", _png(), "image/png")})
        key = created.json()["key"]
        signed_url = (await async_client_with_db.get("/api/v1/images/signed-url", params={"key": key})).json()["url"]
        await async_client_with_db.delete(f"/api/v1/images/{key}")

    # Token still verifies, but the asset is gone → 404, not 403.
    response = await async_client_with_db.get(signed_url)

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.usefixtures("media_storage")
async def test_signed_fetch_cache_reflects_remaining_lifetime(
    async_client_with_db: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _make_user(db_session, "signcache@example.com")
    with as_user(user):
        created = await async_client_with_db.post("/api/v1/images", files={"file": ("cover.png", _png(), "image/png")})
    signed_url = (
        await async_client_with_db.get("/api/v1/images/signed-url", params={"key": created.json()["key"]})
    ).json()["url"]

    ttl = get_settings().media_signed_url_ttl_seconds
    # Fetch late in the token's life: max-age must track the *remaining* lifetime, not the full TTL,
    # so a cached copy can't outlive the URL.
    with time_machine.travel(datetime.now(UTC) + timedelta(seconds=ttl - 100)):
        response = await async_client_with_db.get(signed_url)

    assert response.status_code == status.HTTP_200_OK
    max_age = int(response.headers["cache-control"].split("max-age=")[1])
    assert 0 < max_age <= 100
