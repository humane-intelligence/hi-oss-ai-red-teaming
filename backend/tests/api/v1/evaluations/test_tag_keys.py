"""Integration tests for the `/v1/evaluations/{id}/tag-keys` allowed-tag-key router."""

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from app.core.evaluations.models import Evaluation
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def updater_role(db_session: AsyncSession) -> Role:
    """Read + update + `evaluation_groups:manage` (break-glass lifts the in-group write gate)."""
    role = Role(
        name="tagkey-updater",
        description="eval read/update + manage",
        permissions=[
            Permission.EVALUATIONS_READ.value,
            Permission.EVALUATIONS_UPDATE.value,
            Permission.EVALUATION_GROUPS_MANAGE.value,
        ],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def plain_updater_role(db_session: AsyncSession) -> Role:
    """Read + update but NO `evaluation_groups:manage` — so the object-scope write gate applies."""
    role = Role(
        name="tagkey-plain-updater",
        description="eval read/update, no manage",
        permissions=[Permission.EVALUATIONS_READ.value, Permission.EVALUATIONS_UPDATE.value],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def reader_role(db_session: AsyncSession) -> Role:
    role = Role(name="tagkey-reader", description="read-only", permissions=[Permission.EVALUATIONS_READ.value])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def evaluation(db_session: AsyncSession) -> Evaluation:
    group = await persist_evaluation_group(db_session)
    row = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


def _auth(caller: User) -> dict:
    return {"Authorization": f"Bearer {_token(caller)}"}


async def test_list_is_empty_by_default(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, reader_role, email="tk-list@example.com")
    response = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}/tag-keys", headers=_auth(caller))
    assert response.status_code == status.HTTP_200_OK
    assert response.json() == []


async def test_add_key_returns_201_and_appears_in_list(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="tk-add@example.com")
    add = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/tag-keys", json={"key": "env"}, headers=_auth(caller)
    )
    assert add.status_code == status.HTTP_201_CREATED
    assert add.json()["key"] == "env"

    listed = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}/tag-keys", headers=_auth(caller))
    assert [k["key"] for k in listed.json()] == ["env"]


async def test_add_sets_a_location_header(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="tk-loc@example.com")
    add = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/tag-keys", json={"key": "env"}, headers=_auth(caller)
    )
    assert add.status_code == status.HTTP_201_CREATED
    assert add.headers["Location"] == f"/api/v1/evaluations/{evaluation.id}/tag-keys/env"


async def test_add_invalid_key_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="tk-bad@example.com")
    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/tag-keys", json={"key": "bad key"}, headers=_auth(caller)
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_remove_key_returns_204_and_clears_it(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="tk-del@example.com")
    url = f"/api/v1/evaluations/{evaluation.id}/tag-keys"
    await auth_db_client.post(url, json={"key": "env"}, headers=_auth(caller))
    removed = await auth_db_client.delete(f"{url}/env", headers=_auth(caller))
    assert removed.status_code == status.HTTP_204_NO_CONTENT
    listed = await auth_db_client.get(url, headers=_auth(caller))
    assert listed.json() == []


async def test_add_forbidden_without_manage_or_ownership(
    auth_db_client: AsyncClient, db_session: AsyncSession, plain_updater_role: Role, evaluation: Evaluation
) -> None:
    # Holds evaluations:update (passes the coarse permission dep) but is neither the group owner nor a
    # break-glass manager, so the object-scope gate in authorize_evaluation_mutation must reject with
    # 403 (not 404 — the public+approved group is visible). This is the object gate the updater_role
    # tests bypass via evaluation_groups:manage. (Single request: a rolled-back mutation would wipe
    # the shared-transaction fixture, so add/remove are covered as separate tests.)
    caller = await _caller(db_session, plain_updater_role, email="tk-nomanage-add@example.com")
    add = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/tag-keys", json={"key": "env"}, headers=_auth(caller)
    )
    assert add.status_code == status.HTTP_403_FORBIDDEN


async def test_remove_forbidden_without_manage_or_ownership(
    auth_db_client: AsyncClient, db_session: AsyncSession, plain_updater_role: Role, evaluation: Evaluation
) -> None:
    # Same object gate on the DELETE verb (its own wiring point). Authorization runs before the key
    # lookup, so it 403s regardless of whether the key exists.
    caller = await _caller(db_session, plain_updater_role, email="tk-nomanage-del@example.com")
    removed = await auth_db_client.delete(f"/api/v1/evaluations/{evaluation.id}/tag-keys/env", headers=_auth(caller))
    assert removed.status_code == status.HTTP_403_FORBIDDEN


async def test_reader_can_list_but_not_add(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, reader_role, email="tk-reader@example.com")
    assert (
        await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}/tag-keys", headers=_auth(caller))
    ).status_code == status.HTTP_200_OK
    forbidden = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/tag-keys", json={"key": "env"}, headers=_auth(caller)
    )
    assert forbidden.status_code == status.HTTP_403_FORBIDDEN


async def test_keys_are_scoped_to_their_evaluation(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    # Two evaluations in the same transaction: without the `evaluation_id` predicate on every tag-key
    # query, A's schema would list, authorize and delete under B (a cross-evaluation restriction bypass).
    caller = await _caller(db_session, updater_role, email="tk-scope@example.com")
    other = Evaluation(
        title="Other",
        description="d",
        evaluation_group_id=evaluation.evaluation_group_id,
        created_by_id=evaluation.created_by_id,
    )
    db_session.add(other)
    await db_session.flush()

    assert (
        await auth_db_client.post(
            f"/api/v1/evaluations/{evaluation.id}/tag-keys", json={"key": "env"}, headers=_auth(caller)
        )
    ).status_code == status.HTTP_201_CREATED

    listed_other = await auth_db_client.get(f"/api/v1/evaluations/{other.id}/tag-keys", headers=_auth(caller))
    assert [row["key"] for row in listed_other.json()] == []
    # The same key is addable on the sibling (no cross-evaluation uniqueness) and removing it there
    # must not touch A's row.
    assert (
        await auth_db_client.post(
            f"/api/v1/evaluations/{other.id}/tag-keys", json={"key": "env"}, headers=_auth(caller)
        )
    ).status_code == status.HTTP_201_CREATED
    assert (
        await auth_db_client.delete(f"/api/v1/evaluations/{other.id}/tag-keys/env", headers=_auth(caller))
    ).status_code == status.HTTP_204_NO_CONTENT
    listed_source = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}/tag-keys", headers=_auth(caller))
    assert [row["key"] for row in listed_source.json()] == ["env"]


async def test_restriction_toggles_through_the_api_and_round_trips(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    # Every other restriction test sets the flag on the ORM object; this one proves the PATCH field
    # is wired and surfaced, so dropping it from the payload/response can't pass unnoticed.
    caller = await _caller(db_session, updater_role, email="tk-toggle@example.com")
    patched = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}", json={"tags_restricted": True}, headers=_auth(caller)
    )
    assert patched.status_code == status.HTTP_200_OK
    assert patched.json()["tags_restricted"] is True
    fetched = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}", headers=_auth(caller))
    assert fetched.json()["tags_restricted"] is True


async def test_tagging_defaults_on_and_toggles_through_the_api(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    # The console hides every tagging surface on this flag alone, so both its default (on, so
    # pre-flag evaluations keep tagging) and the PATCH round-trip are contract, not incidental.
    caller = await _caller(db_session, updater_role, email="tk-enabled-toggle@example.com")
    fetched = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}", headers=_auth(caller))
    assert fetched.json()["tags_enabled"] is True
    patched = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}", json={"tags_enabled": False}, headers=_auth(caller)
    )
    assert patched.status_code == status.HTTP_200_OK
    assert patched.json()["tags_enabled"] is False
    refetched = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}", headers=_auth(caller))
    assert refetched.json()["tags_enabled"] is False


async def test_tag_keys_stay_editable_while_tagging_is_disabled(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    # The key schema is admin setup, independent of the runtime gate: an admin can prepare (or keep)
    # the allowed keys with tagging off, so re-enabling it doesn't mean re-authoring the schema.
    caller = await _caller(db_session, updater_role, email="tk-disabled-schema@example.com")
    evaluation.tags_enabled = False
    db_session.add(evaluation)
    await db_session.flush()
    created = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/tag-keys", json={"key": "env"}, headers=_auth(caller)
    )
    assert created.status_code == status.HTTP_201_CREATED
    listed = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}/tag-keys", headers=_auth(caller))
    assert [row["key"] for row in listed.json()] == ["env"]
