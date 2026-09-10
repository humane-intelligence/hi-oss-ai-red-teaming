"""Integration tests for the notifications router — flat `/api/v1/notifications`."""

from uuid import UUID

import pytest
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import User
from app.core.auth.roles import Permission
from app.core.notifications.enums import NotificationObjectType
from app.core.notifications.services.notifications import create_notification
from tests.api.v1.conftest import bearer as _auth
from tests.api.v1.conftest import caller_with

pytestmark = pytest.mark.integration

_URL = "/api/v1/notifications"
_READ = Permission.NOTIFICATIONS_READ
_UPDATE = Permission.NOTIFICATIONS_UPDATE


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


async def _mark(client: AsyncClient, user: User, ids: list[str], *, read: bool) -> Response:
    return await client.post(f"{_URL}/mark", json={"ids": ids, "read": read}, headers=_auth(user))


async def _get(client: AsyncClient, user: User, notification_id: UUID) -> dict:
    response = await client.get(f"{_URL}/{notification_id}", headers=_auth(user))
    assert response.status_code == status.HTTP_200_OK
    return response.json()


async def test_list_scopes_to_caller_and_projects_shape(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _READ, email="a@example.com")
    other = await caller_with(db_session, _READ, email="b@example.com")
    mine = await create_notification(
        db_session,
        user_id=caller.id,
        name="Evaluation approved",
        description="Nice",
        object_type=NotificationObjectType.EVALUATION,
        object_id=caller.id,
    )
    await create_notification(db_session, user_id=other.id, name="Theirs")

    response = await auth_db_client.get(_URL, headers=_auth(caller))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    (item,) = body["items"]
    assert item["id"] == str(mine.id)
    assert item["name"] == "Evaluation approved"
    assert item["read"] is False
    assert item["read_at"] is None
    assert item["object_type"] == "evaluation"
    assert item["user_id"] == str(caller.id)


async def test_list_read_filter_yields_unread_count(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _READ, _UPDATE, email="a@example.com")
    read = await create_notification(db_session, user_id=caller.id, name="read")
    await create_notification(db_session, user_id=caller.id, name="unread")
    await create_notification(db_session, user_id=caller.id, name="also-unread")
    await _mark(auth_db_client, caller, [str(read.id)], read=True)

    response = await auth_db_client.get(_URL, params={"read": "false"}, headers=_auth(caller))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 2


async def test_list_paginates(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _READ, email="a@example.com")
    for name in ("one", "two", "three"):
        await create_notification(db_session, user_id=caller.id, name=name)

    first = await auth_db_client.get(_URL, params={"limit": 2, "offset": 0}, headers=_auth(caller))
    second = await auth_db_client.get(_URL, params={"limit": 2, "offset": 2}, headers=_auth(caller))

    assert first.json()["total"] == second.json()["total"] == 3
    assert len(first.json()["items"]) == 2
    assert len(second.json()["items"]) == 1
    # Two disjoint pages cover the whole set (order is not asserted — same-tx rows tie on created_at).
    ids = {item["id"] for item in first.json()["items"]} | {item["id"] for item in second.json()["items"]}
    assert len(ids) == 3


async def test_list_rejects_limit_over_max(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _READ, email="a@example.com")

    response = await auth_db_client.get(_URL, params={"limit": 101}, headers=_auth(caller))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)


async def test_list_filters_by_object_type(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _READ, email="a@example.com")
    await create_notification(
        db_session, user_id=caller.id, name="eval", object_type=NotificationObjectType.EVALUATION, object_id=caller.id
    )
    await create_notification(db_session, user_id=caller.id, name="plain")

    response = await auth_db_client.get(_URL, params={"object_type": "evaluation"}, headers=_auth(caller))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["object_type"] == "evaluation"


async def test_get_own_notification_ok(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _READ, email="a@example.com")
    note = await create_notification(db_session, user_id=caller.id, name="Hi")

    response = await auth_db_client.get(f"{_URL}/{note.id}", headers=_auth(caller))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == str(note.id)


async def test_get_other_users_notification_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _READ, email="a@example.com")
    other = await caller_with(db_session, _READ, email="b@example.com")
    theirs = await create_notification(db_session, user_id=other.id, name="Theirs")

    response = await auth_db_client.get(f"{_URL}/{theirs.id}", headers=_auth(caller))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_mark_specific_ids_read(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _READ, _UPDATE, email="a@example.com")
    first = await create_notification(db_session, user_id=caller.id, name="one")
    second = await create_notification(db_session, user_id=caller.id, name="two")

    response = await _mark(auth_db_client, caller, [str(first.id)], read=True)

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"updated": 1}
    assert (await _get(auth_db_client, caller, first.id))["read"] is True
    assert (await _get(auth_db_client, caller, second.id))["read"] is False


async def test_mark_all_when_ids_empty(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _READ, _UPDATE, email="a@example.com")
    first = await create_notification(db_session, user_id=caller.id, name="one")
    second = await create_notification(db_session, user_id=caller.id, name="two")

    response = await _mark(auth_db_client, caller, [], read=True)

    assert response.json() == {"updated": 2}
    assert (await _get(auth_db_client, caller, first.id))["read"] is True
    assert (await _get(auth_db_client, caller, second.id))["read"] is True


async def test_mark_unread_clears_state(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _READ, _UPDATE, email="a@example.com")
    note = await create_notification(db_session, user_id=caller.id, name="one")
    await _mark(auth_db_client, caller, [str(note.id)], read=True)

    response = await _mark(auth_db_client, caller, [str(note.id)], read=False)

    assert response.json() == {"updated": 1}
    assert (await _get(auth_db_client, caller, note.id))["read"] is False


async def test_list_requires_read_permission_403(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _UPDATE, email="a@example.com")

    response = await auth_db_client.get(_URL, headers=_auth(caller))

    assert response.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(response, status.HTTP_403_FORBIDDEN)


async def test_mark_requires_update_permission_403(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await caller_with(db_session, _READ, email="a@example.com")

    response = await _mark(auth_db_client, caller, [], read=True)

    assert response.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(response, status.HTTP_403_FORBIDDEN)


async def test_list_requires_auth_401(auth_db_client: AsyncClient) -> None:
    response = await auth_db_client.get(_URL)

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    _assert_problem(response, status.HTTP_401_UNAUTHORIZED)
