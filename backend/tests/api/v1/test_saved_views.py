"""Integration tests for the saved-views router — flat `/api/v1/saved-views`."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.auth.models import User
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from app.core.saved_views.models import SavedView
from tests.api.v1.conftest import bearer as _auth
from tests.api.v1.conftest import make_role

pytestmark = pytest.mark.integration

_SAVED_VIEW_PERMS = [
    Permission.SAVED_VIEWS_READ.value,
    Permission.SAVED_VIEWS_CREATE.value,
    Permission.SAVED_VIEWS_UPDATE.value,
    Permission.SAVED_VIEWS_DELETE.value,
]

_URL = "/api/v1/saved-views"


async def _caller(db_session: AsyncSession, *, email: str, permissions: list[str] | None = None) -> User:
    role = await make_role(db_session, list(_SAVED_VIEW_PERMS if permissions is None else permissions))
    return await create_user_service(db_session, email=email, roles=[role])


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


async def _create(client: AsyncClient, user: User, body: dict) -> Response:
    return await client.post(_URL, json=body, headers=_auth(user))


async def _view_deleted_at(db_session: AsyncSession, view_id: str) -> object:
    """Raw fetch of a view's `deleted_at`, bypassing the live-row filter."""
    result = await db_session.execute(select(SavedView.deleted_at).where(SavedView.id == UUID(view_id)))
    return result.scalar_one()


async def test_create_returns_201_and_location(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    state = {"order_by": "-created_at", "filters": {"status": "published"}, "hidden_columns": ["created_at"]}

    response = await _create(auth_db_client, caller, {"resource": "evaluations", "name": "Newest", "state": state})

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert response.headers["Location"] == f"/api/v1/saved-views/{body['id']}"
    assert body["resource"] == "evaluations"
    assert body["name"] == "Newest"
    # The envelope is normalized: sent fields plus defaults for the rest.
    assert body["state"] == {**state, "search": None, "limit": None, "offset": None}
    assert body["created_by_id"] == str(caller.id)


async def test_create_defaults_empty_state(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")

    response = await _create(auth_db_client, caller, {"resource": "users", "name": "All"})

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["state"] == {
        "order_by": None,
        "filters": {},
        "hidden_columns": [],
        "search": None,
        "limit": None,
        "offset": None,
    }


async def test_create_duplicate_name_same_resource_conflicts(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    caller = await _caller(db_session, email="a@example.com")
    body = {"resource": "evaluations", "name": "Dup"}
    assert (await _create(auth_db_client, caller, body)).status_code == status.HTTP_201_CREATED

    response = await _create(auth_db_client, caller, body)

    assert response.status_code == status.HTTP_409_CONFLICT
    _assert_problem(response, status.HTTP_409_CONFLICT)


async def test_create_same_name_different_resource_ok(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    assert (
        await _create(auth_db_client, caller, {"resource": "evaluations", "name": "Same"})
    ).status_code == status.HTTP_201_CREATED

    response = await _create(auth_db_client, caller, {"resource": "users", "name": "Same"})

    assert response.status_code == status.HTTP_201_CREATED


async def test_create_rejects_unknown_resource_422(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")

    response = await _create(auth_db_client, caller, {"resource": "not-a-list", "name": "X"})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)


async def test_create_rejects_oversized_state(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    oversized = {"filters": {"blob": "x" * (64 * 1024 + 1)}}

    response = await _create(auth_db_client, caller, {"resource": "evaluations", "name": "Big", "state": oversized})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)


async def test_create_rejects_unknown_state_key(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")

    response = await _create(auth_db_client, caller, {"resource": "evaluations", "name": "X", "state": {"bogus": 1}})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_create_rejects_malformed_order_by(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    body = {"resource": "evaluations", "name": "X", "state": {"order_by": "not a token!"}}

    response = await _create(auth_db_client, caller, body)

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_list_is_scoped_to_owner(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _caller(db_session, email="owner@example.com")
    other = await _caller(db_session, email="other@example.com")
    await _create(auth_db_client, owner, {"resource": "evaluations", "name": "Mine"})

    owner_list = await auth_db_client.get(_URL, headers=_auth(owner))
    other_list = await auth_db_client.get(_URL, headers=_auth(other))

    assert owner_list.json()["total"] == 1
    assert other_list.json()["total"] == 0


async def test_list_filters_by_resource(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    await _create(auth_db_client, caller, {"resource": "evaluations", "name": "E"})
    await _create(auth_db_client, caller, {"resource": "users", "name": "U"})

    response = await auth_db_client.get(_URL, params={"resource": "users"}, headers=_auth(caller))

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["resource"] == "users"


async def test_list_orders_by_name(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    for name in ("Charlie", "Alpha", "Bravo"):
        await _create(auth_db_client, caller, {"resource": "evaluations", "name": name})

    response = await auth_db_client.get(_URL, headers=_auth(caller))

    assert [item["name"] for item in response.json()["items"]] == ["Alpha", "Bravo", "Charlie"]


async def test_get_others_view_is_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _caller(db_session, email="owner@example.com")
    other = await _caller(db_session, email="other@example.com")
    view_id = (await _create(auth_db_client, owner, {"resource": "evaluations", "name": "Mine"})).json()["id"]

    response = await auth_db_client.get(f"{_URL}/{view_id}", headers=_auth(other))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_update_renames_and_replaces_state(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    view_id = (
        await _create(
            auth_db_client, caller, {"resource": "evaluations", "name": "Old", "state": {"filters": {"status": "x"}}}
        )
    ).json()["id"]

    response = await auth_db_client.patch(
        f"{_URL}/{view_id}", json={"name": "New", "state": {"order_by": "name"}}, headers=_auth(caller)
    )

    body = response.json()
    assert response.status_code == status.HTTP_200_OK
    assert body["name"] == "New"
    # Wholesale replace: the old filters are gone, order_by set, the rest defaulted.
    assert body["state"] == {
        "order_by": "name",
        "filters": {},
        "hidden_columns": [],
        "search": None,
        "limit": None,
        "offset": None,
    }


async def test_update_rename_into_existing_conflicts(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    await _create(auth_db_client, caller, {"resource": "evaluations", "name": "Taken"})
    view_id = (await _create(auth_db_client, caller, {"resource": "evaluations", "name": "Free"})).json()["id"]

    response = await auth_db_client.patch(f"{_URL}/{view_id}", json={"name": "Taken"}, headers=_auth(caller))

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_update_rejects_explicit_null(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    view_id = (await _create(auth_db_client, caller, {"resource": "evaluations", "name": "V"})).json()["id"]

    null_name = await auth_db_client.patch(f"{_URL}/{view_id}", json={"name": None}, headers=_auth(caller))
    null_state = await auth_db_client.patch(f"{_URL}/{view_id}", json={"state": None}, headers=_auth(caller))

    assert null_name.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert null_state.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_update_rejects_invalid_state(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    # PATCH reuses the same envelope validation as create (sibling-op coverage).
    caller = await _caller(db_session, email="a@example.com")
    view_id = (await _create(auth_db_client, caller, {"resource": "evaluations", "name": "V"})).json()["id"]

    response = await auth_db_client.patch(f"{_URL}/{view_id}", json={"state": {"bogus": 1}}, headers=_auth(caller))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_update_others_view_is_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _caller(db_session, email="owner@example.com")
    other = await _caller(db_session, email="other@example.com")
    view_id = (await _create(auth_db_client, owner, {"resource": "evaluations", "name": "Mine"})).json()["id"]

    response = await auth_db_client.patch(f"{_URL}/{view_id}", json={"name": "Hijacked"}, headers=_auth(other))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_delete_soft_deletes_then_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    view_id = (await _create(auth_db_client, caller, {"resource": "evaluations", "name": "V"})).json()["id"]

    delete = await auth_db_client.delete(f"{_URL}/{view_id}", headers=_auth(caller))

    assert delete.status_code == status.HTTP_204_NO_CONTENT
    assert await _view_deleted_at(db_session, view_id) is not None
    assert (
        await auth_db_client.get(f"{_URL}/{view_id}", headers=_auth(caller))
    ).status_code == status.HTTP_404_NOT_FOUND


async def test_delete_frees_the_name(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    body = {"resource": "evaluations", "name": "Reusable"}
    view_id = (await _create(auth_db_client, caller, body)).json()["id"]
    await auth_db_client.delete(f"{_URL}/{view_id}", headers=_auth(caller))

    response = await _create(auth_db_client, caller, body)

    assert response.status_code == status.HTTP_201_CREATED


async def test_delete_others_view_is_404(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _caller(db_session, email="owner@example.com")
    other = await _caller(db_session, email="other@example.com")
    view_id = (await _create(auth_db_client, owner, {"resource": "evaluations", "name": "Mine"})).json()["id"]

    response = await auth_db_client.delete(f"{_URL}/{view_id}", headers=_auth(other))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def _restore(client: AsyncClient, user: User, view_id: str) -> Response:
    return await client.post(f"{_URL}/{view_id}/restore", headers=_auth(user))


async def _backdate_tombstone(db_session: AsyncSession, view_id: str, *, days: int) -> None:
    await db_session.execute(
        update(SavedView)
        .where(col(SavedView.id) == UUID(view_id))
        .values(deleted_at=datetime.now(UTC) - timedelta(days=days))
    )


async def test_restore_brings_a_deleted_view_back(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    view_id = (await _create(auth_db_client, caller, {"resource": "evaluations", "name": "V"})).json()["id"]
    await auth_db_client.delete(f"{_URL}/{view_id}", headers=_auth(caller))

    response = await _restore(auth_db_client, caller, view_id)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None
    assert await _view_deleted_at(db_session, view_id) is None
    assert (await auth_db_client.get(f"{_URL}/{view_id}", headers=_auth(caller))).status_code == status.HTTP_200_OK


async def test_restore_conflicts_when_the_name_was_reused(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # The unique index covers live rows only, so deleting frees the name — a
    # restore that would resurrect the duplicate has to 409, not 500.
    caller = await _caller(db_session, email="a@example.com")
    body = {"resource": "evaluations", "name": "Reused"}
    view_id = (await _create(auth_db_client, caller, body)).json()["id"]
    await auth_db_client.delete(f"{_URL}/{view_id}", headers=_auth(caller))
    await _create(auth_db_client, caller, body)

    response = await _restore(auth_db_client, caller, view_id)

    _assert_problem(response, status.HTTP_409_CONFLICT)
    assert await _view_deleted_at(db_session, view_id) is not None


async def test_restore_is_404_for_another_users_view(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    owner = await _caller(db_session, email="owner@example.com")
    view_id = (await _create(auth_db_client, owner, {"resource": "evaluations", "name": "Mine"})).json()["id"]
    await auth_db_client.delete(f"{_URL}/{view_id}", headers=_auth(owner))
    stranger = await _caller(db_session, email="stranger@example.com")

    response = await _restore(auth_db_client, stranger, view_id)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)
    assert await _view_deleted_at(db_session, view_id) is not None


async def test_restore_is_404_outside_the_window(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    view_id = (await _create(auth_db_client, caller, {"resource": "evaluations", "name": "Stale"})).json()["id"]
    await auth_db_client.delete(f"{_URL}/{view_id}", headers=_auth(caller))
    await _backdate_tombstone(db_session, view_id, days=30)

    response = await _restore(auth_db_client, caller, view_id)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_deleted_listing_returns_only_tombstones(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    live_id = (await _create(auth_db_client, caller, {"resource": "evaluations", "name": "Live"})).json()["id"]
    gone_id = (await _create(auth_db_client, caller, {"resource": "evaluations", "name": "Gone"})).json()["id"]
    await auth_db_client.delete(f"{_URL}/{gone_id}", headers=_auth(caller))

    response = await auth_db_client.get(f"{_URL}?deleted=true", headers=_auth(caller))

    assert response.status_code == status.HTTP_200_OK
    items = response.json()["items"]
    assert [item["id"] for item in items] == [gone_id]
    assert items[0]["deleted_at"] is not None
    assert live_id not in [item["id"] for item in items]


async def test_a_null_deleter_tombstone_is_neither_listed_nor_restorable(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # The deleter scope is redundant with the owner scope except here: a tombstone
    # predating `deleted_by_id` carries NULL, and views have no break-glass, so it is
    # unreachable by anyone. Without the scope the owner would see and restore it.
    caller = await _caller(db_session, email="a@example.com")
    view_id = (await _create(auth_db_client, caller, {"resource": "evaluations", "name": "Legacy"})).json()["id"]
    await auth_db_client.delete(f"{_URL}/{view_id}", headers=_auth(caller))
    await db_session.execute(update(SavedView).where(col(SavedView.id) == UUID(view_id)).values(deleted_by_id=None))

    listed = await auth_db_client.get(f"{_URL}?deleted=true", headers=_auth(caller))
    restored = await _restore(auth_db_client, caller, view_id)

    assert [item["id"] for item in listed.json()["items"]] == []
    _assert_problem(restored, status.HTTP_404_NOT_FOUND)


async def test_restore_requires_the_delete_permission(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    caller = await _caller(db_session, email="a@example.com")
    view_id = (await _create(auth_db_client, caller, {"resource": "evaluations", "name": "V"})).json()["id"]
    await auth_db_client.delete(f"{_URL}/{view_id}", headers=_auth(caller))
    reader = await _caller(db_session, email="reader@example.com", permissions=[Permission.SAVED_VIEWS_READ.value])

    response = await _restore(auth_db_client, reader, view_id)

    _assert_problem(response, status.HTTP_403_FORBIDDEN)
