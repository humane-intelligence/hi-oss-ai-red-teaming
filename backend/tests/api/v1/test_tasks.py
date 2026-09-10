"""Integration tests for the task router — nested `/scenarios/{scenario_id}/tasks`."""

from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import Scenario
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def updater_role(db_session: AsyncSession) -> Role:
    """Read + update + `evaluation_groups:manage` — lifts the owner-or-manage gate for mechanics tests."""
    role = Role(
        name="evaluator",
        description="read + update + manage",
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
async def owner_updater_role(db_session: AsyncSession) -> Role:
    """Read + update only — subject to the parent-group owner-or-manage write gate."""
    role = Role(
        name="evaluator-no-manage",
        description="read + update",
        permissions=[Permission.EVALUATIONS_READ.value, Permission.EVALUATIONS_UPDATE.value],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def reader_role(db_session: AsyncSession) -> Role:
    role = Role(name="eval-reader", description="read-only", permissions=[Permission.EVALUATIONS_READ.value])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _scenario_for(
    db_session: AsyncSession,
    *,
    owner_id: UUID | None = None,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
) -> Scenario:
    group = await persist_evaluation_group(db_session, access_level=access_level, created_by_id=owner_id)
    evaluation = Evaluation(
        title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    scenario = Scenario(evaluation_id=evaluation.id, name="S", description="d", position=0)
    db_session.add(scenario)
    await db_session.flush()
    await db_session.refresh(scenario)
    return scenario


@pytest_asyncio.fixture
async def scenario(db_session: AsyncSession) -> Scenario:
    return await _scenario_for(db_session)


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


async def _create(client: AsyncClient, token: str, scenario_id: object, name: str) -> dict:
    response = await client.post(
        f"/api/v1/scenarios/{scenario_id}/tasks",
        json={"name": name, "description": "d"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()


def _assert_problem(response: Response, expected_status: int) -> None:
    """Assert the RFC 7807 envelope: `application/problem+json` + `status`/`title`."""
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


async def test_create_returns_201_and_location(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, scenario: Scenario
) -> None:
    caller = await _caller(db_session, updater_role, email="c@example.com")

    response = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario.id}/tasks",
        json={"name": "First", "description": "d"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["scenario_id"] == str(scenario.id)
    assert body["name"] == "First"
    assert response.headers["Location"] == f"/api/v1/scenarios/{scenario.id}/tasks/{body['id']}"


async def test_list_returns_tasks(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, scenario: Scenario
) -> None:
    caller = await _caller(db_session, updater_role, email="list@example.com")
    token = _token(caller)
    await _create(auth_db_client, token, scenario.id, "A")
    await _create(auth_db_client, token, scenario.id, "B")

    response = await auth_db_client.get(
        f"/api/v1/scenarios/{scenario.id}/tasks", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 2
    assert {item["name"] for item in body["items"]} == {"A", "B"}


async def test_list_visible_scenario_without_tasks_returns_empty_page(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, scenario: Scenario
) -> None:
    # The list-first path runs the 404 check only on an empty page, so a visible
    # scenario with no tasks must still return a 200 empty page, not a 404.
    caller = await _caller(db_session, updater_role, email="empty@example.com")

    response = await auth_db_client.get(
        f"/api/v1/scenarios/{scenario.id}/tasks", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []


async def test_get_returns_task(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, scenario: Scenario
) -> None:
    caller = await _caller(db_session, updater_role, email="get@example.com")
    token = _token(caller)
    created = await _create(auth_db_client, token, scenario.id, "T")

    response = await auth_db_client.get(
        f"/api/v1/scenarios/{scenario.id}/tasks/{created['id']}", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == created["id"]


async def test_update_changes_content(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, scenario: Scenario
) -> None:
    caller = await _caller(db_session, updater_role, email="patch@example.com")
    token = _token(caller)
    created = await _create(auth_db_client, token, scenario.id, "before")

    response = await auth_db_client.patch(
        f"/api/v1/scenarios/{scenario.id}/tasks/{created['id']}",
        json={"name": "after"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "after"


async def test_delete_soft_deletes(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, scenario: Scenario
) -> None:
    caller = await _caller(db_session, updater_role, email="del@example.com")
    token = _token(caller)
    created = await _create(auth_db_client, token, scenario.id, "doomed")
    headers = {"Authorization": f"Bearer {token}"}

    deleted = await auth_db_client.delete(f"/api/v1/scenarios/{scenario.id}/tasks/{created['id']}", headers=headers)
    follow_up = await auth_db_client.get(f"/api/v1/scenarios/{scenario.id}/tasks/{created['id']}", headers=headers)

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND


async def test_list_private_scenario_not_visible_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role
) -> None:
    # The scenario exists but its group is invitation-only and owned by someone else;
    # a reader without manage can't see it, so list_tasks scopes it out (total == 0)
    # and the empty-page branch resolves it to a 404 — same as an unknown id, but
    # this exercises the *private-but-existing* path at the API layer.
    scenario = await _scenario_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    caller = await _caller(db_session, reader_role, email="outsider@example.com")

    response = await auth_db_client.get(
        f"/api/v1/scenarios/{scenario.id}/tasks", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_unknown_scenario_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role
) -> None:
    caller = await _caller(db_session, updater_role, email="missing@example.com")

    response = await auth_db_client.get(
        f"/api/v1/scenarios/{uuid4()}/tasks", headers={"Authorization": f"Bearer {_token(caller)}"}
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_create_on_unknown_scenario_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role
) -> None:
    caller = await _caller(db_session, updater_role, email="missing-create@example.com")

    response = await auth_db_client.post(
        f"/api/v1/scenarios/{uuid4()}/tasks",
        json={"name": "x", "description": "d"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_owner_without_manage_can_create_in_own_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, owner_updater_role: Role
) -> None:
    caller = await _caller(db_session, owner_updater_role, email="owner@example.com")
    scenario = await _scenario_for(db_session, owner_id=caller.id, access_level=EvaluationGroupAccessLevel.PUBLIC)

    response = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario.id}/tasks",
        json={"name": "mine", "description": "d"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED


async def test_non_owner_without_manage_forbidden_on_public_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, owner_updater_role: Role
) -> None:
    caller = await _caller(db_session, owner_updater_role, email="stranger@example.com")
    scenario = await _scenario_for(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)  # owned by a throwaway

    response = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario.id}/tasks",
        json={"name": "nope", "description": "d"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(response, status.HTTP_403_FORBIDDEN)


async def _restore(client: AsyncClient, token: str, scenario_id: object, task_id: str) -> Response:
    return await client.post(
        f"/api/v1/scenarios/{scenario_id}/tasks/{task_id}/restore",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.integration
async def test_restore_task_brings_it_back(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, scenario: Scenario
) -> None:
    caller = await _caller(db_session, updater_role, email="restore-task@example.com")
    token = _token(caller)
    scenario_id = scenario.id
    task = await _create(auth_db_client, token, scenario_id, "back-again")
    await auth_db_client.delete(
        f"/api/v1/scenarios/{scenario_id}/tasks/{task['id']}", headers={"Authorization": f"Bearer {token}"}
    )

    response = await _restore(auth_db_client, token, scenario_id, task["id"])

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None
    follow_up = await auth_db_client.get(
        f"/api/v1/scenarios/{scenario_id}/tasks/{task['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    assert follow_up.status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_restore_task_404s_for_another_users_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, owner_updater_role: Role
) -> None:
    # The in-group write gate passes for the owner, but the deleter scope does not:
    # the restore reaches exactly what the deleted listing offered, and no more.
    owner = await _caller(db_session, owner_updater_role, email="task-own-deletes@example.com")
    admin = await _caller(db_session, updater_role, email="task-moderator@example.com")
    scenario = await _scenario_for(db_session, owner_id=owner.id)
    scenario_id = scenario.id
    task = await _create(auth_db_client, _token(owner), scenario_id, "moderated")
    await auth_db_client.delete(
        f"/api/v1/scenarios/{scenario_id}/tasks/{task['id']}",
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )

    response = await _restore(auth_db_client, _token(owner), scenario_id, task["id"])

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_restore_task_break_glass_reaches_another_users_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, owner_updater_role: Role
) -> None:
    owner = await _caller(db_session, owner_updater_role, email="task-deleted-by-owner@example.com")
    admin = await _caller(db_session, updater_role, email="task-break-glass@example.com")
    scenario = await _scenario_for(db_session, owner_id=owner.id)
    scenario_id = scenario.id
    task = await _create(auth_db_client, _token(owner), scenario_id, "revived")
    await auth_db_client.delete(
        f"/api/v1/scenarios/{scenario_id}/tasks/{task['id']}",
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )

    response = await _restore(auth_db_client, _token(admin), scenario_id, task["id"])

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None


@pytest.mark.integration
async def test_restore_task_404s_when_its_scenario_is_deleted(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, scenario: Scenario
) -> None:
    # Restoring the scenario is what brings its tasks back, so a task under a dead
    # scenario is not individually restorable.
    caller = await _caller(db_session, updater_role, email="task-dead-parent@example.com")
    token = _token(caller)
    scenario_id = scenario.id
    task = await _create(auth_db_client, token, scenario_id, "orphan")
    await auth_db_client.delete(
        f"/api/v1/scenarios/{scenario_id}/tasks/{task['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    scenario.soft_delete(caller.id)
    await db_session.flush()

    response = await _restore(auth_db_client, token, scenario_id, task["id"])

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


@pytest.mark.integration
async def test_deleted_task_listing_requires_update_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role, scenario: Scenario
) -> None:
    caller = await _caller(db_session, reader_role, email="reader-deleted-task@example.com")

    response = await auth_db_client.get(
        f"/api/v1/scenarios/{scenario.id}/tasks?deleted=true",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    _assert_problem(response, status.HTTP_403_FORBIDDEN)


@pytest.mark.integration
async def test_deleted_task_listing_needs_parent_group_write_access(
    auth_db_client: AsyncClient, db_session: AsyncSession, owner_updater_role: Role, scenario: Scenario
) -> None:
    # The tombstone view is the restore surface, so it runs the gate the restore runs —
    # parity with the model-assignment list. The live list stays a plain read.
    caller = await _caller(db_session, owner_updater_role, email="task-no-group-write@example.com")

    response = await auth_db_client.get(
        f"/api/v1/scenarios/{scenario.id}/tasks?deleted=true",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    _assert_problem(response, status.HTTP_403_FORBIDDEN)
    live = await auth_db_client.get(
        f"/api/v1/scenarios/{scenario.id}/tasks", headers={"Authorization": f"Bearer {_token(caller)}"}
    )
    assert live.status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_deleted_task_listing_serves_the_tombstone(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, scenario: Scenario
) -> None:
    caller = await _caller(db_session, updater_role, email="deleted-task-list@example.com")
    token = _token(caller)
    scenario_id = scenario.id
    kept = await _create(auth_db_client, token, scenario_id, "kept")
    dropped = await _create(auth_db_client, token, scenario_id, "dropped")
    await auth_db_client.delete(
        f"/api/v1/scenarios/{scenario_id}/tasks/{dropped['id']}", headers={"Authorization": f"Bearer {token}"}
    )

    response = await auth_db_client.get(
        f"/api/v1/scenarios/{scenario_id}/tasks?deleted=true", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == status.HTTP_200_OK
    listed = [item["id"] for item in response.json()["items"]]
    assert listed == [dropped["id"]]
    assert kept["id"] not in listed


@pytest.mark.integration
async def test_restore_task_404s_through_another_scenario(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, scenario: Scenario
) -> None:
    # `scenario_id` is part of the restore lookup: write access to one scenario must not
    # reach another's tombstone by swapping the path segment.
    caller = await _caller(db_session, updater_role, email="cross-parent-task@example.com")
    token = _token(caller)
    victim_id = scenario.id
    task = await _create(auth_db_client, token, victim_id, "not-yours")
    await auth_db_client.delete(
        f"/api/v1/scenarios/{victim_id}/tasks/{task['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    attacker_scenario = await _scenario_for(db_session)

    response = await _restore(auth_db_client, token, attacker_scenario.id, task["id"])

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


@pytest.mark.integration
async def test_deleted_task_listing_scopes_to_the_callers_own_deletes(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    owner_updater_role: Role,
) -> None:
    # The group is the owner's, since the tombstone view runs the parent-group write gate.
    owner = await _caller(db_session, owner_updater_role, email="task-outsider@example.com")
    deleter = await _caller(db_session, updater_role, email="task-deleter@example.com")
    scenario = await _scenario_for(db_session, owner_id=owner.id)
    scenario_id = scenario.id
    task = await _create(auth_db_client, _token(deleter), scenario_id, "deleted-by-admin")
    await auth_db_client.delete(
        f"/api/v1/scenarios/{scenario_id}/tasks/{task['id']}",
        headers={"Authorization": f"Bearer {_token(deleter)}"},
    )

    mine = await auth_db_client.get(
        f"/api/v1/scenarios/{scenario_id}/tasks?deleted=true",
        headers={"Authorization": f"Bearer {_token(deleter)}"},
    )
    # Same permission, no break-glass, deleted nothing → sees nothing.
    scoped = await auth_db_client.get(
        f"/api/v1/scenarios/{scenario_id}/tasks?deleted=true",
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )

    assert [item["id"] for item in mine.json()["items"]] == [task["id"]]
    assert scoped.json()["items"] == []
