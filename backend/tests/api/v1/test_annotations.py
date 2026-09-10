"""Integration tests for the annotation router — flat `/api/v1/annotations`.

Owns the HTTP contract: status codes, the response projection (including the embedded
label), request validation the route layer adds, and the permission gate in both
directions per verb — plus the per-role matrix, since the red teamer holding *no*
annotation key at all is the surprising half of this entity's RBAC. Branch logic lives
in the service tests.
"""

from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.annotations.label_catalog import curated_label_id
from app.core.annotations.services.annotation_labels import sync_annotation_labels
from app.core.auth.roles import Permission
from tests.api.v1.conftest import bearer
from tests.api.v1.conftest import caller_with
from tests.core.annotations.conftest import ConversationTarget
from tests.core.annotations.conftest import persist_conversation_target

pytestmark = pytest.mark.integration

_URL = "/api/v1/annotations"
_JAILBREAK = curated_label_id("jailbreak")

_FULL = (Permission.ANNOTATIONS_READ, Permission.ANNOTATIONS_CREATE, Permission.ANNOTATIONS_DELETE)


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


def _body(target: ConversationTarget, *, label_id: UUID | None = _JAILBREAK, text: str | None = None) -> dict:
    payload: dict = {"message_id": str(target.messages[0].id)}
    if label_id is not None:
        payload["label_id"] = str(label_id)
    if text is not None:
        payload["text"] = text
    return payload


async def test_post_returns_201_with_location_and_the_embedded_label(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The happy path: an annotator labels a message of a conversation they do not own."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_FULL)

    response = await auth_db_client.post(_URL, json=_body(target), headers=bearer(annotator))

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert response.headers["Location"] == f"{_URL}/{body['id']}"
    assert body["message_id"] == str(target.messages[0].id)
    assert body["conversation_id"] == str(target.conversation.id)
    assert body["created_by_id"] == str(annotator.id)
    # The label is embedded, not merely referenced — a retired one is unresolvable otherwise.
    assert body["label"] == {
        "id": str(_JAILBREAK),
        "key": "jailbreak",
        "name": "Jailbreak",
        "is_custom": False,
    }


async def test_post_accepts_an_adhoc_label(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_FULL)

    response = await auth_db_client.post(
        _URL, json=_body(target, label_id=None, text="prompt injection"), headers=bearer(annotator)
    )

    body = response.json()
    assert response.status_code == status.HTTP_201_CREATED
    # A typed label is no longer stored on the annotation: it names a label row scoped to the
    # caller, which the response embeds like any other — flagged `is_custom` so the picker can
    # tell it from the curated vocabulary.
    assert body["label"]["name"] == "prompt injection"
    assert body["label"]["is_custom"] is True
    assert body["label"]["key"] is None


async def test_post_rejects_both_or_neither_label_kind(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """The exactly-one rule is a 422 at the edge, never a 500 from deeper in the write path."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_FULL)
    headers = bearer(annotator)

    both = await auth_db_client.post(_URL, json=_body(target, text="jailbreak"), headers=headers)
    neither = await auth_db_client.post(_URL, json=_body(target, label_id=None), headers=headers)

    assert both.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(both, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert neither.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_post_is_idempotent_on_a_repeat(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """A double-click is the same intent — the same row comes back, not a 409."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_FULL)
    headers = bearer(annotator)

    first = await auth_db_client.post(_URL, json=_body(target), headers=headers)
    second = await auth_db_client.post(_URL, json=_body(target), headers=headers)

    assert second.status_code == status.HTTP_201_CREATED
    assert second.json()["id"] == first.json()["id"]


async def test_post_404s_on_an_unknown_label(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_FULL)

    response = await auth_db_client.post(_URL, json=_body(target, label_id=uuid4()), headers=bearer(annotator))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_get_list_serves_every_author(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """Reads are shared: a second reader sees the first author's annotation."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    author = await caller_with(db_session, *_FULL)
    reader = await caller_with(db_session, Permission.ANNOTATIONS_READ)
    created = await auth_db_client.post(_URL, json=_body(target), headers=bearer(author))

    listed = await auth_db_client.get(_URL, params={"message_id": str(target.messages[0].id)}, headers=bearer(reader))
    fetched = await auth_db_client.get(f"{_URL}/{created.json()['id']}", headers=bearer(reader))

    assert listed.status_code == status.HTTP_200_OK
    assert [item["id"] for item in listed.json()["items"]] == [created.json()["id"]]
    assert fetched.status_code == status.HTTP_200_OK
    assert fetched.json()["created_by_id"] == str(author.id)


async def test_delete_refuses_another_author_with_403(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """403, not 404 — shared reads already told the caller the row exists."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    author = await caller_with(db_session, *_FULL)
    other = await caller_with(db_session, *_FULL)
    # Both headers are built up front: the 403 rolls its request back, which expires every
    # instance in this shared session, so a later `bearer(author)` would lazy-load the user.
    author_headers, other_headers = bearer(author), bearer(other)
    created = await auth_db_client.post(_URL, json=_body(target), headers=author_headers)
    annotation_id = created.json()["id"]

    forbidden = await auth_db_client.delete(f"{_URL}/{annotation_id}", headers=other_headers)
    allowed = await auth_db_client.delete(f"{_URL}/{annotation_id}", headers=author_headers)

    assert forbidden.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(forbidden, status.HTTP_403_FORBIDDEN)
    assert allowed.status_code == status.HTTP_204_NO_CONTENT


async def test_delete_then_restore_round_trips(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_FULL)
    headers = bearer(annotator)
    annotation_id = (await auth_db_client.post(_URL, json=_body(target), headers=headers)).json()["id"]
    await auth_db_client.delete(f"{_URL}/{annotation_id}", headers=headers)

    tombstones = await auth_db_client.get(_URL, params={"deleted": "true"}, headers=headers)
    restored = await auth_db_client.post(f"{_URL}/{annotation_id}/restore", headers=headers)

    assert [item["id"] for item in tombstones.json()["items"]] == [annotation_id]
    assert restored.status_code == status.HTTP_200_OK
    assert restored.json()["deleted_at"] is None


async def test_restore_409s_when_the_slot_was_retaken(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_FULL)
    headers = bearer(annotator)
    annotation_id = (await auth_db_client.post(_URL, json=_body(target), headers=headers)).json()["id"]
    await auth_db_client.delete(f"{_URL}/{annotation_id}", headers=headers)
    await auth_db_client.post(_URL, json=_body(target), headers=headers)

    response = await auth_db_client.post(f"{_URL}/{annotation_id}/restore", headers=headers)

    assert response.status_code == status.HTTP_409_CONFLICT
    _assert_problem(response, status.HTTP_409_CONFLICT)


async def test_verbs_require_their_own_permission(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """Each verb gates on its own key, in both directions, and unauthenticated is 401."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    full = bearer(await caller_with(db_session, *_FULL))
    read_only = bearer(await caller_with(db_session, Permission.ANNOTATIONS_READ))
    annotation_id = (await auth_db_client.post(_URL, json=_body(target), headers=full)).json()["id"]

    assert (await auth_db_client.get(_URL, headers=read_only)).status_code == status.HTTP_200_OK
    assert (await auth_db_client.post(_URL, json=_body(target), headers=read_only)).status_code == (
        status.HTTP_403_FORBIDDEN
    )
    assert (await auth_db_client.delete(f"{_URL}/{annotation_id}", headers=read_only)).status_code == (
        status.HTTP_403_FORBIDDEN
    )

    unauthorized = await auth_db_client.get(_URL)
    assert unauthorized.status_code == status.HTTP_401_UNAUTHORIZED
    _assert_problem(unauthorized, status.HTTP_401_UNAUTHORIZED)


async def test_list_rejects_a_limit_above_the_cap(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """The shared `PaginationDep` caps `limit` at 100; this route must not widen it."""
    caller = await caller_with(db_session, Permission.ANNOTATIONS_READ)

    response = await auth_db_client.get(f"{_URL}?limit=101", headers=bearer(caller))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert any("limit" in error["loc"] for error in response.json()["errors"])
