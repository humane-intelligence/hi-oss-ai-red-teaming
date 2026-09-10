"""Route tests for `/api/v1/terms`."""

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
from app.core.terms.service import accept_terms
from app.core.terms.service import publish_terms
from tests.api.v1.conftest import PROBLEM_CT
from tests.api.v1.conftest import bearer
from tests.api.v1.conftest import caller_with


@pytest.mark.integration
async def test_get_current_terms_returns_the_published_document(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    document = await publish_terms(db_session, version="1.0", content="# Terms\n\nBe careful.")

    response = await auth_db_client.get("/api/v1/terms/current")

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["id"] == str(document.id)
    assert body["version"] == "1.0"
    assert body["content"] == "# Terms\n\nBe careful."
    assert body["published_at"] is not None


@pytest.mark.integration
async def test_get_current_terms_needs_no_bearer_token(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    await publish_terms(db_session, version="1.0", content="Terms")

    response = await auth_db_client.get("/api/v1/terms/current")

    assert response.status_code == status.HTTP_200_OK
    assert response.headers["cache-control"] == "public, max-age=30"


@pytest.mark.integration
async def test_get_current_terms_is_404_before_anything_is_published(auth_db_client: AsyncClient) -> None:
    response = await auth_db_client.get("/api/v1/terms/current")

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_list_terms_pages_summaries_without_bodies(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await caller_with(db_session, Permission.PLATFORM_SETTINGS_READ)
    document = await publish_terms(db_session, version="1.0", content="First")
    # The history view is behind the consent gate like everything else, so the admin has to have
    # accepted what is published before it answers.
    await accept_terms(db_session, admin, terms_id=document.id)

    response = await auth_db_client.get("/api/v1/terms", headers=bearer(admin))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (body["total"], body["limit"], body["offset"]) == (1, 20, 0)
    assert body["items"][0]["version"] == "1.0"
    assert "content" not in body["items"][0]


@pytest.mark.integration
async def test_get_terms_serves_one_version_to_any_authenticated_caller(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    reader = await caller_with(db_session)
    document = await publish_terms(db_session, version="1.0", content="The text")

    response = await auth_db_client.get(f"/api/v1/terms/{document.id}", headers=bearer(reader))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["content"] == "The text"


@pytest.mark.integration
async def test_get_terms_is_401_without_a_token(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    document = await publish_terms(db_session, version="1.0", content="The text")

    response = await auth_db_client.get(f"/api/v1/terms/{document.id}")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.integration
async def test_get_terms_is_404_for_an_unknown_id(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    reader = await caller_with(db_session)

    response = await auth_db_client.get(f"/api/v1/terms/{uuid4()}", headers=bearer(reader))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"] == PROBLEM_CT


@pytest.mark.integration
async def test_publish_terms_creates_the_version_and_audits_it(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await caller_with(db_session, Permission.PLATFORM_SETTINGS_UPDATE)

    response = await auth_db_client.post(
        "/api/v1/terms",
        headers=bearer(admin),
        json={"version": "1.0", "content": "# Terms\n\nBe careful."},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["version"] == "1.0"
    assert response.headers["location"] == f"/api/v1/terms/{body['id']}"
    result = await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.TERMS_PUBLISH.value))
    row = result.scalar_one()
    assert row.actor_id == admin.id
    assert row.after == {"version": "1.0"}


@pytest.mark.integration
async def test_publish_terms_rejects_a_duplicate_version_on_the_field(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await caller_with(db_session, Permission.PLATFORM_SETTINGS_UPDATE)
    document = await publish_terms(db_session, version="1.0", content="First")
    await accept_terms(db_session, admin, terms_id=document.id)

    response = await auth_db_client.post(
        "/api/v1/terms", headers=bearer(admin), json={"version": "1.0", "content": "Second"}
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["errors"][0]["loc"] == ["body", "version"]


@pytest.mark.integration
@pytest.mark.parametrize("content", ["", "   "])
async def test_publish_terms_rejects_a_blank_body(
    auth_db_client: AsyncClient, db_session: AsyncSession, content: str
) -> None:
    admin = await caller_with(db_session, Permission.PLATFORM_SETTINGS_UPDATE)

    response = await auth_db_client.post(
        "/api/v1/terms", headers=bearer(admin), json={"version": "1.0", "content": content}
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
