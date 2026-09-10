"""Integration tests for the note router — flat `/api/v1/notes`.

Owns the HTTP contract only: status codes, response shape, the permission gate on **both**
directions per verb, request validation the route layer adds, and the wiring each verb derives
for itself (`caller_id` into the author scope, the inline `evaluation_groups:manage` break-glass,
typed query params). Branch logic lives in the service tests.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.annotations.models import Note
from app.core.annotations.schemas import NoteCreate
from app.core.annotations.services.notes import create_note
from app.core.auth.roles import Permission
from tests.api.v1.conftest import bearer
from tests.api.v1.conftest import caller_with
from tests.core.annotations.conftest import ConversationTarget
from tests.core.annotations.conftest import persist_conversation_target

pytestmark = pytest.mark.integration

_URL = "/api/v1/notes"

_NOTE_PERMS = (
    Permission.NOTES_READ,
    Permission.NOTES_CREATE,
    Permission.NOTES_UPDATE,
    Permission.NOTES_DELETE,
)


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


def _body(target: ConversationTarget, *, text: str = "Complies after the reframing.") -> dict:
    return {
        "conversation_id": str(target.conversation.id),
        "message_ids": [str(message.id) for message in target.messages],
        "text": text,
    }


async def test_post_returns_201_with_location_and_the_selection_ids(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The happy path: an annotator notes a conversation they do not own."""
    target = await persist_conversation_target(db_session, message_count=2)
    annotator = await caller_with(db_session, *_NOTE_PERMS)

    response = await auth_db_client.post(_URL, json=_body(target), headers=bearer(annotator))

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert response.headers["Location"] == f"{_URL}/{body['id']}"
    assert body["conversation_id"] == str(target.conversation.id)
    assert body["evaluation_id"] == str(target.evaluation.id)
    assert body["evaluation_group_id"] == str(target.group.id)
    assert body["created_by_id"] == str(annotator.id)
    assert body["text"] == "Complies after the reframing."
    # Order-insensitive on purpose: the fixture seeds assistant-only messages in one
    # transaction, so they tie on `created_at` and `role` and the `id` tiebreak decides.
    # The intra-turn order is pinned where a real prompt+reply pair exists, in
    # `tests/core/annotations/test_models.py`.
    assert set(body["message_ids"]) == {str(message.id) for message in target.messages}
    assert "messages" not in body


async def test_post_requires_notes_create(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """Both directions of the create gate, plus the unauthenticated case."""
    target = await persist_conversation_target(db_session)
    allowed = await caller_with(db_session, *_NOTE_PERMS)
    denied = await caller_with(db_session, Permission.NOTES_READ)
    allowed_headers, denied_headers = bearer(allowed), bearer(denied)

    assert (await auth_db_client.post(_URL, json=_body(target), headers=allowed_headers)).status_code == (
        status.HTTP_201_CREATED
    )
    forbidden = await auth_db_client.post(_URL, json=_body(target), headers=denied_headers)
    unauthorized = await auth_db_client.post(_URL, json=_body(target))

    assert forbidden.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(forbidden, status.HTTP_403_FORBIDDEN)
    assert unauthorized.status_code == status.HTTP_401_UNAUTHORIZED
    _assert_problem(unauthorized, status.HTTP_401_UNAUTHORIZED)


async def test_post_on_unreachable_conversation_is_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    annotator = await caller_with(db_session, *_NOTE_PERMS)

    response = await auth_db_client.post(
        _URL,
        json={"conversation_id": str(uuid4()), "message_ids": [str(uuid4())], "text": "nowhere"},
        headers=bearer(annotator),
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


@pytest.mark.parametrize(
    ("payload_patch", "offending_field"),
    [
        pytest.param({"message_ids": []}, "message_ids", id="empty_message_ids"),
        pytest.param({"text": ""}, "text", id="blank_text"),
        pytest.param({"text": "x" * 10_001}, "text", id="oversized_text"),
    ],
)
async def test_post_rejects_invalid_payloads(
    auth_db_client: AsyncClient, db_session: AsyncSession, payload_patch: dict, offending_field: str
) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)

    response = await auth_db_client.post(_URL, json=_body(target) | payload_patch, headers=bearer(annotator))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    # Naming the field is what separates "rejected for this reason" from "rejected".
    assert any(offending_field in error["loc"] for error in response.json()["errors"])


async def test_post_rejects_duplicate_message_ids(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    duplicated = [str(target.messages[0].id), str(target.messages[0].id)]

    response = await auth_db_client.post(
        _URL, json=_body(target) | {"message_ids": duplicated}, headers=bearer(annotator)
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("message_ids" in error["loc"] for error in response.json()["errors"])


async def test_get_list_returns_page_shape(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    await auth_db_client.post(_URL, json=_body(target), headers=bearer(annotator))

    response = await auth_db_client.get(_URL, headers=bearer(annotator))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert body["limit"] > 0
    assert body["offset"] == 0
    assert len(body["items"]) == 1
    assert body["items"][0]["message_ids"] == [str(target.messages[0].id)]


async def test_list_paginates_by_limit_and_offset(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """`limit` / `offset` reach the service, and `total` stays the unpaginated count."""
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    for index in range(3):
        await auth_db_client.post(_URL, json=_body(target) | {"text": f"note {index}"}, headers=bearer(annotator))

    first = await auth_db_client.get(f"{_URL}?limit=2&offset=0", headers=bearer(annotator))
    last = await auth_db_client.get(f"{_URL}?limit=2&offset=2", headers=bearer(annotator))

    assert [len(first.json()["items"]), len(last.json()["items"])] == [2, 1]
    assert first.json()["total"] == last.json()["total"] == 3
    assert first.json()["limit"] == 2
    assert last.json()["offset"] == 2
    # Disjoint pages — a handler ignoring `offset` would return the same two rows twice.
    assert {item["id"] for item in first.json()["items"]}.isdisjoint(item["id"] for item in last.json()["items"])


async def test_list_rejects_a_limit_above_the_cap(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """The shared `PaginationDep` caps `limit` at 100; this route must not widen it."""
    annotator = await caller_with(db_session, *_NOTE_PERMS)

    response = await auth_db_client.get(f"{_URL}?limit=101", headers=bearer(annotator))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("limit" in error["loc"] for error in response.json()["errors"])


async def test_get_detail_returns_the_note(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=bearer(annotator))).json()

    response = await auth_db_client.get(f"{_URL}/{created['id']}", headers=bearer(annotator))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == created["id"]


async def test_foreign_note_reads_as_404_not_403(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """Another user's note must not even confirm its existence."""
    target = await persist_conversation_target(db_session)
    author = await caller_with(db_session, *_NOTE_PERMS)
    other = await caller_with(db_session, *_NOTE_PERMS)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=bearer(author))).json()

    detail = await auth_db_client.get(f"{_URL}/{created['id']}", headers=bearer(other))
    listing = await auth_db_client.get(_URL, headers=bearer(other))

    assert detail.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(detail, status.HTTP_404_NOT_FOUND)
    assert listing.json()["total"] == 0


@pytest.mark.parametrize("verb", ["patch", "delete"])
async def test_foreign_note_cannot_be_written_without_break_glass(
    auth_db_client: AsyncClient, db_session: AsyncSession, verb: str
) -> None:
    """Holding `notes:update`/`delete` must not reach another author's note.

    The author predicate is derived per handler, so each write verb needs its own
    negative rep — the break-glass test above only covers the lifted direction.
    """
    target = await persist_conversation_target(db_session)
    author = await caller_with(db_session, *_NOTE_PERMS)
    other = await caller_with(db_session, *_NOTE_PERMS)
    author_headers = bearer(author)
    headers = bearer(other)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=author_headers)).json()

    if verb == "patch":
        response = await auth_db_client.patch(f"{_URL}/{created['id']}", json={"text": "hijacked"}, headers=headers)
    else:
        response = await auth_db_client.delete(f"{_URL}/{created['id']}", headers=headers)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)
    survivor = await auth_db_client.get(f"{_URL}/{created['id']}", headers=author_headers)
    assert survivor.status_code == status.HTTP_200_OK
    assert survivor.json()["text"] == created["text"]


@pytest.mark.parametrize("verb", ["list", "detail", "patch", "delete"])
async def test_every_verb_rejects_an_unauthenticated_caller(
    auth_db_client: AsyncClient, db_session: AsyncSession, verb: str
) -> None:
    """Each verb authenticates for itself — the 403 reps all send a token, so they can't see this."""
    target = await persist_conversation_target(db_session)
    author = await caller_with(db_session, *_NOTE_PERMS)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=bearer(author))).json()

    if verb == "list":
        response = await auth_db_client.get(_URL)
    elif verb == "detail":
        response = await auth_db_client.get(f"{_URL}/{created['id']}")
    elif verb == "patch":
        response = await auth_db_client.patch(f"{_URL}/{created['id']}", json={"text": "x"})
    else:
        response = await auth_db_client.delete(f"{_URL}/{created['id']}")

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    _assert_problem(response, status.HTTP_401_UNAUTHORIZED)


@pytest.mark.parametrize("verb", ["list", "detail", "patch", "delete"])
async def test_each_verb_derives_the_break_glass_itself(
    auth_db_client: AsyncClient, db_session: AsyncSession, verb: str
) -> None:
    """`evaluation_groups:manage` is derived inline per handler, so every verb needs its own rep."""
    target = await persist_conversation_target(db_session)
    author = await caller_with(db_session, *_NOTE_PERMS)
    manager = await caller_with(db_session, *_NOTE_PERMS, Permission.EVALUATION_GROUPS_MANAGE)
    note = await create_note(
        db_session,
        NoteCreate(
            conversation_id=target.conversation.id,
            message_ids=[target.messages[0].id],
            text="authored by someone else",
        ),
        caller_id=author.id,
    )
    headers = bearer(manager)

    if verb == "list":
        response = await auth_db_client.get(_URL, headers=headers)
        assert response.json()["total"] == 1
    elif verb == "detail":
        response = await auth_db_client.get(f"{_URL}/{note.id}", headers=headers)
        assert response.status_code == status.HTTP_200_OK
    elif verb == "patch":
        response = await auth_db_client.patch(f"{_URL}/{note.id}", json={"text": "x"}, headers=headers)
        assert response.status_code == status.HTTP_200_OK
    else:
        response = await auth_db_client.delete(f"{_URL}/{note.id}", headers=headers)
        assert response.status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.parametrize(
    ("verb", "required"),
    [
        pytest.param("list", Permission.NOTES_READ, id="list"),
        pytest.param("detail", Permission.NOTES_READ, id="detail"),
        pytest.param("patch", Permission.NOTES_UPDATE, id="patch"),
        pytest.param("delete", Permission.NOTES_DELETE, id="delete"),
    ],
)
async def test_each_verb_denies_a_caller_without_its_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession, verb: str, required: Permission
) -> None:
    """The denied direction for every verb — a gate tested only on the happy caller is half a test."""
    target = await persist_conversation_target(db_session)
    author = await caller_with(db_session, *_NOTE_PERMS)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=bearer(author))).json()
    denied = await caller_with(db_session, *[p for p in _NOTE_PERMS if p is not required])
    headers = bearer(denied)

    if verb == "list":
        response = await auth_db_client.get(_URL, headers=headers)
    elif verb == "detail":
        response = await auth_db_client.get(f"{_URL}/{created['id']}", headers=headers)
    elif verb == "patch":
        response = await auth_db_client.patch(f"{_URL}/{created['id']}", json={"text": "x"}, headers=headers)
    else:
        response = await auth_db_client.delete(f"{_URL}/{created['id']}", headers=headers)

    assert response.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(response, status.HTTP_403_FORBIDDEN)


@pytest.mark.parametrize(
    ("query", "expected_status"),
    [
        pytest.param("message_id=not-a-uuid", status.HTTP_422_UNPROCESSABLE_CONTENT, id="message_id_uuid"),
        pytest.param("created_from=not-a-date", status.HTTP_422_UNPROCESSABLE_CONTENT, id="created_from_datetime"),
        pytest.param("created_from=2026-01-01T00:00:00Z", status.HTTP_200_OK, id="created_from_accepted"),
    ],
)
async def test_list_binds_typed_query_params(
    auth_db_client: AsyncClient, db_session: AsyncSession, query: str, expected_status: int
) -> None:
    """The param→filter wiring the service tests cannot see."""
    annotator = await caller_with(db_session, *_NOTE_PERMS)

    response = await auth_db_client.get(f"{_URL}?{query}", headers=bearer(annotator))

    assert response.status_code == expected_status


async def test_list_binds_order_by_to_the_service(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """`order_by` is a route-level param, so a hard-coded default would pass every other test."""
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    older = (await auth_db_client.post(_URL, json=_body(target) | {"text": "older"}, headers=bearer(annotator))).json()
    newer = (await auth_db_client.post(_URL, json=_body(target) | {"text": "newer"}, headers=bearer(annotator))).json()
    # One transaction per request here, so `created_at` genuinely differs; the rows are
    # re-stamped anyway so the assertion cannot fall back on the `id` tiebreak.
    await db_session.execute(
        update(Note).where(col(Note.id) == UUID(older["id"])).values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    await db_session.execute(
        update(Note).where(col(Note.id) == UUID(newer["id"])).values(created_at=datetime(2026, 2, 1, tzinfo=UTC))
    )
    await db_session.commit()

    ascending = await auth_db_client.get(f"{_URL}?order_by=created_at", headers=bearer(annotator))
    descending = await auth_db_client.get(f"{_URL}?order_by=-created_at", headers=bearer(annotator))
    rejected = await auth_db_client.get(f"{_URL}?order_by=text", headers=bearer(annotator))

    assert [item["id"] for item in ascending.json()["items"]] == [older["id"], newer["id"]]
    assert [item["id"] for item in descending.json()["items"]] == [newer["id"], older["id"]]
    assert rejected.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(rejected, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("order_by" in error["loc"] for error in rejected.json()["errors"])


async def test_list_filters_by_message_id(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session, message_count=2)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    await auth_db_client.post(
        _URL,
        json=_body(target) | {"message_ids": [str(target.messages[0].id)]},
        headers=bearer(annotator),
    )

    matched = await auth_db_client.get(f"{_URL}?message_id={target.messages[0].id}", headers=bearer(annotator))
    unmatched = await auth_db_client.get(f"{_URL}?message_id={target.messages[1].id}", headers=bearer(annotator))

    assert matched.json()["total"] == 1
    assert unmatched.json()["total"] == 0


async def test_patch_updates_text(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=bearer(annotator))).json()

    response = await auth_db_client.patch(
        f"{_URL}/{created['id']}", json={"text": "revised"}, headers=bearer(annotator)
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["text"] == "revised"
    # The response body alone would also pass for a handler that never persisted.
    # (`updated_at` is deliberately not asserted to move: the whole test runs in one
    # transaction, and `func.now()` is the transaction timestamp.)
    reread = await auth_db_client.get(f"{_URL}/{created['id']}", headers=bearer(annotator))
    assert reread.json()["text"] == "revised"


async def test_patch_of_a_missing_note_is_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    """PATCH declares 404 in `responses=`; nothing else in the suite produces one from it."""
    annotator = await caller_with(db_session, *_NOTE_PERMS)

    response = await auth_db_client.patch(f"{_URL}/{uuid4()}", json={"text": "x"}, headers=bearer(annotator))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


@pytest.mark.parametrize(
    ("payload", "offending_field"),
    [
        pytest.param({"text": None}, "text", id="explicit_null_text"),
        pytest.param({"message_ids": [str(uuid4())]}, "message_ids", id="unknown_field"),
        pytest.param({"text": "x" * 10_001}, "text", id="oversized_text"),
    ],
)
async def test_patch_rejects_invalid_payloads(
    auth_db_client: AsyncClient, db_session: AsyncSession, payload: dict, offending_field: str
) -> None:
    """`message_ids` is not silently ignored — the selection is fixed, so sending it is a 422.

    The `loc` assertion is what pins that: a 422 raised for any other reason would
    otherwise satisfy the status check and leave `extra="forbid"` untested.
    """
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=bearer(annotator))).json()

    response = await auth_db_client.patch(f"{_URL}/{created['id']}", json=payload, headers=bearer(annotator))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any(offending_field in error["loc"] for error in response.json()["errors"])


async def test_delete_returns_204_then_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    # Mint the token once, up front: a `@transactional` handler that raises (the second
    # DELETE's 404) rolls the session back, which expires every instance in it — a later
    # `bearer(annotator)` would then have to lazy-load `user.roles` from sync code.
    headers = bearer(annotator)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=headers)).json()

    first = await auth_db_client.delete(f"{_URL}/{created['id']}", headers=headers)
    second = await auth_db_client.delete(f"{_URL}/{created['id']}", headers=headers)
    gone = await auth_db_client.get(f"{_URL}/{created['id']}", headers=headers)

    assert first.status_code == status.HTTP_204_NO_CONTENT
    assert second.status_code == status.HTTP_404_NOT_FOUND
    assert gone.status_code == status.HTTP_404_NOT_FOUND


async def _backdate_tombstone(db_session: AsyncSession, note_id: str, *, days: int) -> None:
    """Backdate a tombstone past the restore window."""
    await db_session.execute(
        update(Note).where(col(Note.id) == UUID(note_id)).values(deleted_at=datetime.now(UTC) - timedelta(days=days))
    )


async def test_restore_brings_a_deleted_note_back(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    headers = bearer(annotator)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=headers)).json()
    await auth_db_client.delete(f"{_URL}/{created['id']}", headers=headers)

    restored = await auth_db_client.post(f"{_URL}/{created['id']}/restore", headers=headers)
    readable = await auth_db_client.get(f"{_URL}/{created['id']}", headers=headers)

    assert restored.status_code == status.HTTP_200_OK
    assert restored.json()["deleted_at"] is None
    assert readable.status_code == status.HTTP_200_OK


async def test_restore_is_404_for_a_live_note(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    headers = bearer(annotator)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=headers)).json()

    response = await auth_db_client.post(f"{_URL}/{created['id']}/restore", headers=headers)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_restore_is_404_outside_the_window(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    headers = bearer(annotator)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=headers)).json()
    await auth_db_client.delete(f"{_URL}/{created['id']}", headers=headers)
    await _backdate_tombstone(db_session, created["id"], days=30)

    response = await auth_db_client.post(f"{_URL}/{created['id']}/restore", headers=headers)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_restore_requires_the_delete_permission(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=bearer(annotator))).json()
    await auth_db_client.delete(f"{_URL}/{created['id']}", headers=bearer(annotator))
    reader = await caller_with(db_session, Permission.NOTES_READ, email="reader@example.com")

    response = await auth_db_client.post(f"{_URL}/{created['id']}/restore", headers=bearer(reader))

    _assert_problem(response, status.HTTP_403_FORBIDDEN)


async def test_deleted_listing_returns_the_callers_tombstones(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    headers = bearer(annotator)
    live = (await auth_db_client.post(_URL, json=_body(target, text="kept"), headers=headers)).json()
    doomed = (await auth_db_client.post(_URL, json=_body(target, text="dropped"), headers=headers)).json()
    await auth_db_client.delete(f"{_URL}/{doomed['id']}", headers=headers)

    response = await auth_db_client.get(f"{_URL}?deleted=true", headers=headers)

    assert response.status_code == status.HTTP_200_OK
    items = response.json()["items"]
    assert [item["id"] for item in items] == [doomed["id"]]
    assert items[0]["deleted_by_id"] == str(annotator.id)
    assert live["id"] not in [item["id"] for item in items]


async def test_manager_restores_another_authors_delete(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    # The admin rule, and the positive half of the deleter scope: the break-glass
    # reaches a tombstone the manager did not create.
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    author_headers = bearer(annotator)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=author_headers)).json()
    await auth_db_client.delete(f"{_URL}/{created['id']}", headers=author_headers)
    manager = await caller_with(
        db_session,
        *_NOTE_PERMS,
        Permission.EVALUATION_GROUPS_MANAGE,
        email="fixer@example.com",
    )

    response = await auth_db_client.post(f"{_URL}/{created['id']}/restore", headers=bearer(manager))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None


async def test_author_cannot_restore_a_managers_delete(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    annotator = await caller_with(db_session, *_NOTE_PERMS)
    author_headers = bearer(annotator)
    created = (await auth_db_client.post(_URL, json=_body(target), headers=author_headers)).json()
    manager = await caller_with(
        db_session,
        *_NOTE_PERMS,
        Permission.EVALUATION_GROUPS_MANAGE,
        email="manager@example.com",
    )
    await auth_db_client.delete(f"{_URL}/{created['id']}", headers=bearer(manager))

    response = await auth_db_client.post(f"{_URL}/{created['id']}/restore", headers=author_headers)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)
