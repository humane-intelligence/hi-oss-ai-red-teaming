"""Integration tests for the annotation-label router — flat, read-only `/api/v1/annotation-labels`.

Owns the HTTP contract: the projection, the ordering the picker relies on, pagination, and the
permission gate in both directions. The catalog is not seeded per worker, so each test syncs it.
"""

import pytest
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.annotations.label_catalog import CURATED_ANNOTATION_LABELS
from app.core.annotations.label_catalog import curated_label_id
from app.core.annotations.services.annotation_labels import sync_annotation_labels
from app.core.auth.roles import Permission
from tests.api.v1.conftest import bearer
from tests.api.v1.conftest import caller_with

pytestmark = pytest.mark.integration

_URL = "/api/v1/annotation-labels"


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


async def test_get_returns_the_catalog_ordered_by_display_name(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await sync_annotation_labels(db_session)
    caller = await caller_with(db_session, Permission.ANNOTATIONS_READ)

    response = await auth_db_client.get(_URL, headers=bearer(caller))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == len(CURATED_ANNOTATION_LABELS)
    # Ordered by `name`, not `key` — the picker shows the wording, so it drives the order.
    assert [item["key"] for item in body["items"]] == [
        "bias",
        "hallucination",
        "harmful-instructions",
        "jailbreak",
        "off-policy",
        "pii-leak",
        "refusal",
    ]
    # The id must be the deterministic uuid5 an annotation will FK onto — not just any uuid.
    assert body["items"][0] == {
        "id": str(curated_label_id("bias")),
        "key": "bias",
        "name": "Bias",
        # Curated: the shared vocabulary, not something an annotator typed.
        "is_custom": False,
    }


async def test_get_paginates(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    # `total` is the whole vocabulary even when a page shows two of it, so a client can tell
    # there is more to fetch.
    await sync_annotation_labels(db_session)
    caller = await caller_with(db_session, Permission.ANNOTATIONS_READ)

    response = await auth_db_client.get(_URL, params={"limit": 2, "offset": 1}, headers=bearer(caller))

    body = response.json()
    assert response.status_code == status.HTTP_200_OK
    assert [item["key"] for item in body["items"]] == ["hallucination", "harmful-instructions"]
    assert (body["total"], body["limit"], body["offset"]) == (len(CURATED_ANNOTATION_LABELS), 2, 1)


async def test_get_rejects_a_limit_above_the_cap(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """The shared `PaginationDep` caps `limit` at 100; this route must not widen it."""
    caller = await caller_with(db_session, Permission.ANNOTATIONS_READ)

    response = await auth_db_client.get(f"{_URL}?limit=101", headers=bearer(caller))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("limit" in error["loc"] for error in response.json()["errors"])


async def test_get_requires_annotations_read(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    await sync_annotation_labels(db_session)
    allowed = bearer(await caller_with(db_session, Permission.ANNOTATIONS_READ))
    # A red-teamer holds `notes:*` but no annotation key at all — the vocabulary stays invisible.
    denied = bearer(await caller_with(db_session, Permission.NOTES_READ))

    assert (await auth_db_client.get(_URL, headers=allowed)).status_code == status.HTTP_200_OK
    forbidden = await auth_db_client.get(_URL, headers=denied)
    unauthorized = await auth_db_client.get(_URL)

    assert forbidden.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(forbidden, status.HTTP_403_FORBIDDEN)
    assert unauthorized.status_code == status.HTTP_401_UNAUTHORIZED
    _assert_problem(unauthorized, status.HTTP_401_UNAUTHORIZED)
