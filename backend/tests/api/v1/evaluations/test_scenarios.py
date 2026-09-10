"""Integration tests for the scenario routers — nested `/evaluations/{id}/scenarios` and standalone `/scenarios`."""

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
from app.core.evaluations.models import EvaluationGroup
from app.core.organizations.models import Organization
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def updater_role(db_session: AsyncSession) -> Role:
    """Read + update, plus `evaluation_groups:manage`.

    The manage elevation lifts the in-group-role write gate and the
    read-visibility scope, so the scenario *mechanics* tests below stay authorized
    regardless of the caller's role in the parent group. The gate itself is
    exercised by the dedicated tests using `owner_updater_role` / `reader_role`
    (no manage).
    """
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
    """Read + update only — subject to the parent group's in-group-role write gate."""
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


async def _evaluation_for(
    db_session: AsyncSession,
    *,
    owner_id: UUID | None = None,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
) -> Evaluation:
    group = await persist_evaluation_group(db_session, access_level=access_level, created_by_id=owner_id)
    row = Evaluation(title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


@pytest_asyncio.fixture
async def evaluation(db_session: AsyncSession) -> Evaluation:
    return await _evaluation_for(db_session)


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


async def _create(client: AsyncClient, token: str, evaluation_id: object, name: str) -> dict:
    response = await client.post(
        f"/api/v1/evaluations/{evaluation_id}/scenarios",
        json={"name": name, "description": "d"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()


def _assert_problem(response: Response, expected_status: int) -> None:
    """Assert the RFC 7807 envelope: `application/problem+json` + `status`/`title` mirroring the code."""
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]  # non-empty human-readable summary


async def test_create_returns_201_and_location(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="c@example.com")

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/scenarios",
        json={"name": "First", "description": "d"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["evaluation_id"] == str(evaluation.id)
    assert body["position"] == 0
    assert response.headers["Location"] == f"/api/v1/evaluations/{evaluation.id}/scenarios/{body['id']}"


async def test_list_returns_scenarios_in_position_order(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="list@example.com")
    token = _token(caller)
    await _create(auth_db_client, token, evaluation.id, "A")
    await _create(auth_db_client, token, evaluation.id, "B")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 2
    assert [item["name"] for item in body["items"]] == ["A", "B"]


async def test_list_respects_limit_offset_and_rejects_oversized_limit(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="page@example.com")
    token = _token(caller)
    for name in ("A", "B", "C"):
        await _create(auth_db_client, token, evaluation.id, name)
    headers = {"Authorization": f"Bearer {token}"}

    page = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios?limit=1&offset=1&order_by=position", headers=headers
    )
    too_big = await auth_db_client.get(f"/api/v1/evaluations/{evaluation.id}/scenarios?limit=101", headers=headers)

    assert page.status_code == status.HTTP_200_OK
    page_body = page.json()
    assert page_body["total"] == 3
    assert len(page_body["items"]) == 1
    assert page_body["limit"] == 1
    assert page_body["offset"] == 1
    assert page_body["items"][0]["name"] == "B"  # second of A,B,C by position

    assert too_big.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(too_big, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("limit" in error["loc"] for error in too_big.json()["errors"])


async def test_reorder_swaps_positions(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="reorder@example.com")
    token = _token(caller)
    first = await _create(auth_db_client, token, evaluation.id, "A")
    second = await _create(auth_db_client, token, evaluation.id, "B")

    reorder = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/order",
        json={"scenario_ids": [second["id"], first["id"]]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert reorder.status_code == status.HTTP_200_OK
    assert [item["name"] for item in reorder.json()] == ["B", "A"]
    assert [item["position"] for item in reorder.json()] == [0, 1]


async def test_update_changes_content(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="upd@example.com")
    token = _token(caller)
    scenario = await _create(auth_db_client, token, evaluation.id, "Before")

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/{scenario['id']}",
        json={"name": "After"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "After"
    assert response.json()["description"] == "d"  # untouched


async def test_delete_soft_deletes(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="del@example.com")
    token = _token(caller)
    scenario = await _create(auth_db_client, token, evaluation.id, "Doomed")

    deleted = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/{scenario['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    follow_up = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/{scenario['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND


async def test_standalone_list_returns_scenarios(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="flat@example.com")
    token = _token(caller)
    await _create(auth_db_client, token, evaluation.id, "A")

    response = await auth_db_client.get("/api/v1/scenarios", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] >= 1


async def test_get_unknown_evaluation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role
) -> None:
    caller = await _caller(db_session, updater_role, email="missing@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{uuid4()}/scenarios/{uuid4()}",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_owner_can_create_without_manage(
    auth_db_client: AsyncClient, db_session: AsyncSession, owner_updater_role: Role
) -> None:
    caller = await _caller(db_session, owner_updater_role, email="owner@example.com")
    evaluation = await _evaluation_for(db_session, owner_id=caller.id)

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/scenarios",
        json={"name": "Mine", "description": "d"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED


async def test_create_forbidden_for_non_owner_of_public_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, owner_updater_role: Role
) -> None:
    caller = await _caller(db_session, owner_updater_role, email="intruder-pub@example.com")
    # Public group owned by someone else — visible (so 403, not 404), but not writable.
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.PUBLIC)

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/scenarios",
        json={"name": "Nope", "description": "d"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    _assert_problem(response, status.HTTP_403_FORBIDDEN)


async def test_mutation_not_found_for_non_owner_of_private_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, owner_updater_role: Role
) -> None:
    caller = await _caller(db_session, owner_updater_role, email="intruder-priv@example.com")
    # Private group owned by someone else — invisible, so the gate reads as 404.
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/scenarios",
        json={"name": "Nope", "description": "d"},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_reader_can_read_public_group_not_owned(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, reader_role: Role
) -> None:
    owner = await _caller(db_session, updater_role, email="pub-owner@example.com")
    evaluation = await _evaluation_for(db_session, owner_id=owner.id, access_level=EvaluationGroupAccessLevel.PUBLIC)
    await _create(auth_db_client, _token(owner), evaluation.id, "open")
    reader = await _caller(db_session, reader_role, email="curious@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios", headers={"Authorization": f"Bearer {_token(reader)}"}
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 1


async def test_reader_cannot_get_private_scenario_not_owned(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, reader_role: Role
) -> None:
    # GET-one visibility wiring: an existing-but-hidden scenario reads as missing —
    # distinct from the row-missing 404 (the list arm lives in the org-scope test below).
    owner = await _caller(db_session, updater_role, email="priv-owner@example.com")
    evaluation = await _evaluation_for(
        db_session, owner_id=owner.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    created = await _create(auth_db_client, _token(owner), evaluation.id, "secret")
    reader = await _caller(db_session, reader_role, email="nosy@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/{created['id']}",
        headers={"Authorization": f"Bearer {_token(reader)}"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_scenario_visibility_inherits_org_group_scope(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, reader_role: Role
) -> None:
    # The visibility join (`join_visible_evaluation_group`) reuses `group_visible_to`
    # verbatim, so a scenario under an `organization` group inherits the org arm:
    # an org peer can list it, an outsider gets 404 — no descendant-specific logic.
    org = Organization(name="Acme")
    db_session.add(org)
    await db_session.flush()
    owner = await _caller(db_session, updater_role, email="org-eval-owner@example.com")
    evaluation = await _evaluation_for(
        db_session, owner_id=owner.id, access_level=EvaluationGroupAccessLevel.ORGANIZATION
    )
    group = await db_session.get(EvaluationGroup, evaluation.evaluation_group_id)
    assert group is not None
    group.organization_id = org.id
    await db_session.flush()
    await _create(auth_db_client, _token(owner), evaluation.id, "org-only scenario")

    insider = await _caller(db_session, reader_role, email="org-insider@example.com")
    insider.organization_id = org.id
    outsider = await _caller(db_session, reader_role, email="org-outsider@example.com")
    await db_session.flush()

    insider_list = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios", headers={"Authorization": f"Bearer {_token(insider)}"}
    )
    outsider_list = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios", headers={"Authorization": f"Bearer {_token(outsider)}"}
    )

    assert insider_list.status_code == status.HTTP_200_OK
    assert insider_list.json()["total"] == 1
    assert outsider_list.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(outsider_list, status.HTTP_404_NOT_FOUND)


async def test_update_explicit_null_name_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="null@example.com")
    token = _token(caller)
    scenario = await _create(auth_db_client, token, evaluation.id, "keep")

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/{scenario['id']}",
        json={"name": None},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    # The offending field is reported in some error's `loc`.
    assert any("name" in error["loc"] for error in response.json()["errors"])


async def test_standalone_list_filters_by_evaluation_id(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="filter@example.com")
    token = _token(caller)
    await _create(auth_db_client, token, evaluation.id, "here")
    other_eval = await _evaluation_for(db_session)
    await _create(auth_db_client, token, other_eval.id, "elsewhere")

    response = await auth_db_client.get(
        f"/api/v1/scenarios?evaluation_id={evaluation.id}", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["evaluation_id"] == str(evaluation.id)


async def _create_task(client: AsyncClient, token: str, scenario_id: str, name: str) -> dict:
    response = await client.post(
        f"/api/v1/scenarios/{scenario_id}/tasks",
        json={"name": name, "description": "d"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == status.HTTP_201_CREATED
    return response.json()


async def test_get_scenario_embeds_tasks(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="embed@example.com")
    token = _token(caller)
    scenario = await _create(auth_db_client, token, evaluation.id, "S")
    await _create_task(auth_db_client, token, scenario["id"], "T1")
    await _create_task(auth_db_client, token, scenario["id"], "T2")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/{scenario['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["id"] == scenario["id"]
    assert {task["name"] for task in body["tasks"]} == {"T1", "T2"}
    assert all(task["scenario_id"] == scenario["id"] for task in body["tasks"])


async def test_embed_and_task_list_share_ordering(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    """The scenario-detail embed and the tasks list endpoint must agree on order.

    Both paths order by `TASK_DEFAULT_ORDER`; this guards against re-introducing a
    second, divergent ordering on either side.
    """
    caller = await _caller(db_session, updater_role, email="order@example.com")
    token = _token(caller)
    headers = {"Authorization": f"Bearer {token}"}
    scenario = await _create(auth_db_client, token, evaluation.id, "S")
    for name in ("T1", "T2", "T3"):
        await _create_task(auth_db_client, token, scenario["id"], name)

    detail = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/{scenario['id']}", headers=headers
    )
    listing = await auth_db_client.get(f"/api/v1/scenarios/{scenario['id']}/tasks?limit=100", headers=headers)

    assert detail.status_code == status.HTTP_200_OK
    assert listing.status_code == status.HTTP_200_OK
    embed_ids = [task["id"] for task in detail.json()["tasks"]]
    list_ids = [task["id"] for task in listing.json()["items"]]
    assert embed_ids == list_ids
    assert len(embed_ids) == 3


async def test_create_defaults_required_reviews_to_one(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="rr-default@example.com")

    body = await _create(auth_db_client, _token(caller), evaluation.id, "S")

    assert body["required_reviews"] == 1


async def test_create_with_required_reviews(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="rr-set@example.com")

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/scenarios",
        json={"name": "S", "description": "d", "required_reviews": 3},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["required_reviews"] == 3


async def test_create_rejects_required_reviews_below_one(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="rr-zero@example.com")

    response = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation.id}/scenarios",
        json={"name": "S", "description": "d", "required_reviews": 0},
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("required_reviews" in error["loc"] for error in response.json()["errors"])


async def test_update_changes_required_reviews(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="rr-upd@example.com")
    token = _token(caller)
    scenario = await _create(auth_db_client, token, evaluation.id, "S")

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/{scenario['id']}",
        json={"required_reviews": 3},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["required_reviews"] == 3


async def test_update_rejects_required_reviews_below_one(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="rr-upd-zero@example.com")
    token = _token(caller)
    scenario = await _create(auth_db_client, token, evaluation.id, "S")

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/scenarios/{scenario['id']}",
        json={"required_reviews": 0},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)


async def test_scenario_list_stays_lean_without_tasks(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="lean@example.com")
    token = _token(caller)
    scenario = await _create(auth_db_client, token, evaluation.id, "S")
    await _create_task(auth_db_client, token, scenario["id"], "T")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == status.HTTP_200_OK
    assert "tasks" not in response.json()["items"][0]


async def _restore(client: AsyncClient, token: str, evaluation_id: object, scenario_id: str) -> Response:
    return await client.post(
        f"/api/v1/evaluations/{evaluation_id}/scenarios/{scenario_id}/restore",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.integration
async def test_restore_scenario_appends_it_last_with_its_tasks(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    # The delete leaves a `position` gap that a later create fills, so returning the
    # scenario to its old slot would give two live scenarios the same position —
    # nothing in the schema forbids that, so the service appends instead.
    caller = await _caller(db_session, updater_role, email="restore-scenario@example.com")
    token = _token(caller)
    evaluation_id = evaluation.id
    doomed = await _create(auth_db_client, token, evaluation_id, "first")
    task = await _create_task(auth_db_client, token, doomed["id"], "keeps-its-task")
    assert doomed["position"] == 0
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/scenarios/{doomed['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    replacement = await _create(auth_db_client, token, evaluation_id, "took-the-slot")
    assert replacement["position"] == 0

    response = await _restore(auth_db_client, token, evaluation_id, doomed["id"])

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["position"] == 1
    assert response.json()["deleted_at"] is None
    tasks = await auth_db_client.get(
        f"/api/v1/scenarios/{doomed['id']}/tasks", headers={"Authorization": f"Bearer {token}"}
    )
    assert [item["id"] for item in tasks.json()["items"]] == [task["id"]]


@pytest.mark.integration
async def test_restore_scenario_404s_when_its_evaluation_is_deleted(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    # A scenario under a dead evaluation is unreachable; restoring the evaluation is
    # what brings it back, so its own restore reads as missing.
    caller = await _caller(db_session, updater_role, email="scenario-dead-parent@example.com")
    token = _token(caller)
    evaluation_id = evaluation.id
    scenario = await _create(auth_db_client, token, evaluation_id, "orphan")
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/scenarios/{scenario['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    evaluation.soft_delete(caller.id)
    await db_session.flush()

    response = await _restore(auth_db_client, token, evaluation_id, scenario["id"])

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


@pytest.mark.integration
async def test_deleted_scenario_listing_requires_update_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession, reader_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, reader_role, email="reader-deleted-scenario@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios?deleted=true",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    _assert_problem(response, status.HTTP_403_FORBIDDEN)


@pytest.mark.integration
async def test_deleted_scenario_listing_needs_parent_group_write_access(
    auth_db_client: AsyncClient, db_session: AsyncSession, owner_updater_role: Role, evaluation: Evaluation
) -> None:
    # The tombstone view is the restore surface, so it runs the gate the restore runs —
    # parity with the model-assignment list. Without it this caller pages through rows
    # every Restore would then 403 on. The live list stays a plain read.
    caller = await _caller(db_session, owner_updater_role, email="scenario-no-group-write@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios?deleted=true",
        headers={"Authorization": f"Bearer {_token(caller)}"},
    )

    _assert_problem(response, status.HTTP_403_FORBIDDEN)
    live = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/scenarios", headers={"Authorization": f"Bearer {_token(caller)}"}
    )
    assert live.status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_deleted_scenario_listing_serves_the_tombstone(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    caller = await _caller(db_session, updater_role, email="deleted-scenario-list@example.com")
    token = _token(caller)
    evaluation_id = evaluation.id
    kept = await _create(auth_db_client, token, evaluation_id, "kept")
    dropped = await _create(auth_db_client, token, evaluation_id, "dropped")
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/scenarios/{dropped['id']}", headers={"Authorization": f"Bearer {token}"}
    )

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation_id}/scenarios?deleted=true", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == status.HTTP_200_OK
    listed = [item["id"] for item in response.json()["items"]]
    assert listed == [dropped["id"]]
    assert kept["id"] not in listed


@pytest.mark.integration
async def test_restore_scenario_404s_through_another_evaluation(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, evaluation: Evaluation
) -> None:
    # The `evaluation_id` is part of the restore lookup, so a caller with write access to
    # one evaluation cannot reach another's tombstone by swapping the path segment —
    # `authorize_evaluation_mutation` only gates the evaluation *named in the path*.
    caller = await _caller(db_session, updater_role, email="cross-parent-scenario@example.com")
    token = _token(caller)
    victim_id = evaluation.id
    scenario = await _create(auth_db_client, token, victim_id, "not-yours")
    await auth_db_client.delete(
        f"/api/v1/evaluations/{victim_id}/scenarios/{scenario['id']}", headers={"Authorization": f"Bearer {token}"}
    )
    attacker_evaluation = await _evaluation_for(db_session)

    response = await _restore(auth_db_client, token, attacker_evaluation.id, scenario["id"])

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


@pytest.mark.integration
async def test_restore_scenario_404s_for_another_users_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, owner_updater_role: Role
) -> None:
    # The in-group write gate passes for the owner, but the deleter scope does not:
    # the restore reaches exactly what the deleted listing offered, and no more.
    owner = await _caller(db_session, owner_updater_role, email="scenario-own-deletes@example.com")
    admin = await _caller(db_session, updater_role, email="scenario-moderator@example.com")
    evaluation = await _evaluation_for(db_session, owner_id=owner.id)
    evaluation_id = evaluation.id
    scenario = await _create(auth_db_client, _token(owner), evaluation_id, "moderated")
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/scenarios/{scenario['id']}",
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )

    response = await _restore(auth_db_client, _token(owner), evaluation_id, scenario["id"])

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


@pytest.mark.integration
async def test_restore_scenario_break_glass_reaches_another_users_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, updater_role: Role, owner_updater_role: Role
) -> None:
    owner = await _caller(db_session, owner_updater_role, email="scenario-deleted-by-owner@example.com")
    admin = await _caller(db_session, updater_role, email="scenario-break-glass@example.com")
    evaluation = await _evaluation_for(db_session, owner_id=owner.id)
    evaluation_id = evaluation.id
    scenario = await _create(auth_db_client, _token(owner), evaluation_id, "revived")
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/scenarios/{scenario['id']}",
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )

    response = await _restore(auth_db_client, _token(admin), evaluation_id, scenario["id"])

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None


@pytest.mark.integration
async def test_deleted_scenario_listing_scopes_to_the_callers_own_deletes(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    updater_role: Role,
    owner_updater_role: Role,
) -> None:
    # `updater_role` carries the break-glass, so it deletes inside a group it doesn't own;
    # the second caller has the same permission but deleted nothing and must see nothing.
    # The group is the owner's, since the tombstone view runs the parent-group write gate.
    owner = await _caller(db_session, owner_updater_role, email="scenario-outsider@example.com")
    admin = await _caller(db_session, updater_role, email="scenario-deleter@example.com")
    peer = await _caller(db_session, updater_role, email="scenario-peer@example.com")
    evaluation = await _evaluation_for(db_session, owner_id=owner.id)
    evaluation_id = evaluation.id
    scenario = await _create(auth_db_client, _token(admin), evaluation_id, "admin-deleted")
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/scenarios/{scenario['id']}",
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )

    # The deleter holds `evaluation_groups:manage`, which lifts the deleter scope.
    mine = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation_id}/scenarios?deleted=true",
        headers={"Authorization": f"Bearer {_token(admin)}"},
    )
    theirs = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation_id}/scenarios?deleted=true",
        headers={"Authorization": f"Bearer {_token(peer)}"},
    )

    assert [item["id"] for item in mine.json()["items"]] == [scenario["id"]]
    # Same break-glass, so the peer sees it too — the scope is lifted, not per-user.
    assert [item["id"] for item in theirs.json()["items"]] == [scenario["id"]]
    # And the group's own writer, without the break-glass, sees only their own deletes.
    scoped = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation_id}/scenarios?deleted=true",
        headers={"Authorization": f"Bearer {_token(owner)}"},
    )
    assert scoped.json()["items"] == []
