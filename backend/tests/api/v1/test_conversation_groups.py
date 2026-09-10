"""Integration tests for the conversation-group router.

Comparative-testing groups: nested `/evaluations/{id}/conversation-groups`
(+ `/{conversation_group_id}`) plus the flat cross-evaluation `/conversation-groups`, and
the `conversation_group_id` grouping that lands on conversations.
"""

from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from app.core.conversations.models import ConversationGroup
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import Scenario
from tests.api.v1.conftest import build_settings as _settings
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


_CONVERSATION_PERMS = [
    Permission.CONVERSATIONS_READ.value,
    Permission.CONVERSATIONS_CREATE.value,
    Permission.CONVERSATIONS_UPDATE.value,
    Permission.CONVERSATIONS_DELETE.value,
]


@pytest_asyncio.fixture
async def red_teamer_role(db_session: AsyncSession) -> Role:
    role = Role(name="red-teamer", description="conversations CRUD", permissions=list(_CONVERSATION_PERMS))
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def manager_role(db_session: AsyncSession) -> Role:
    role = Role(
        name="conv-manager",
        description="conversations CRUD + manage",
        permissions=[*_CONVERSATION_PERMS, Permission.EVALUATION_GROUPS_MANAGE.value],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def reader_role(db_session: AsyncSession) -> Role:
    role = Role(name="conv-reader", description="read-only", permissions=[Permission.CONVERSATIONS_READ.value])
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


async def _assignment(db_session: AsyncSession, evaluation_id: UUID) -> EvaluationAiModel:
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation_id, model_id=model.id)
    db_session.add(assignment)
    await db_session.flush()
    await db_session.refresh(assignment)
    return assignment


async def _scenario(db_session: AsyncSession, evaluation_id: UUID) -> Scenario:
    row = Scenario(name="S", description="d", evaluation_id=evaluation_id, position=0)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


async def _create_group(client: AsyncClient, token: str, scenario_id: object, body: dict) -> Response:
    return await client.post(f"/api/v1/scenarios/{scenario_id}/conversation-groups", json=body, headers=_auth(token))


async def _group_deleted_at(db_session: AsyncSession, conversation_group_id: str) -> object:
    """Raw fetch of a group's `deleted_at`, bypassing the live-row filter."""
    db_session.expire_all()
    row = (
        await db_session.execute(select(ConversationGroup).where(ConversationGroup.id == UUID(conversation_group_id)))
    ).scalar_one()
    return row.deleted_at


# --- create -------------------------------------------------------------------


async def test_create_returns_201_location_and_grouped_conversations(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="rt@example.com")
    evaluation = await _evaluation_for(db_session)
    first = await _assignment(db_session, evaluation.id)
    second = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)

    response = await _create_group(
        auth_db_client,
        _token(caller),
        scenario.id,
        {
            "name": "compare two",
            "models": [
                {"evaluation_ai_model_id": str(first.id)},
                {"evaluation_ai_model_id": str(second.id), "parameters": {"temperature": 0.9}},
            ],
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["name"] == "compare two"
    assert body["user_id"] == str(caller.id)
    assert body["evaluation_id"] == str(evaluation.id)
    assert len(body["conversations"]) == 2
    # Every member shares the group and is owned by the caller.
    assert {c["conversation_group_id"] for c in body["conversations"]} == {body["id"]}
    assert {c["user_id"] for c in body["conversations"]} == {str(caller.id)}
    by_assignment = {c["evaluation_ai_model_id"]: c for c in body["conversations"]}
    assert by_assignment[str(first.id)]["parameters"] == {}
    assert by_assignment[str(second.id)]["parameters"] == {"temperature": 0.9}
    assert response.headers["Location"] == f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{body['id']}"


async def test_create_rejects_scenario_id_left_in_the_body_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Same contract as the single create: a half-migrated client that adopted the new URL but kept
    # `scenario_id` in the body is told so, instead of having the key silently ignored.
    caller = await _caller(db_session, red_teamer_role, email="stalegroupbody@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)

    response = await _create_group(
        auth_db_client,
        _token(caller),
        scenario.id,
        {
            "name": "stale body",
            "models": [{"evaluation_ai_model_id": str(assignment.id)}],
            "scenario_id": str(scenario.id),
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    # Pin the cause, not just the code: an unrelated validator tightening later would
    # otherwise keep this green while the rejection stopped being about the stale key.
    error = next(e for e in response.json()["errors"] if e["loc"][-1] == "scenario_id")
    assert error["type"] == "extra_forbidden"


async def test_create_same_model_twice_keeps_distinct_parameters(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    """Same assignment listed twice with different params — the "same LLM, different parameters" comparison."""
    caller = await _caller(db_session, red_teamer_role, email="samemodel@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)

    response = await _create_group(
        auth_db_client,
        _token(caller),
        scenario.id,
        {
            "name": "same model, two configs",
            "models": [
                {"evaluation_ai_model_id": str(assignment.id), "parameters": {"temperature": 0.2}},
                {"evaluation_ai_model_id": str(assignment.id), "parameters": {"temperature": 0.9}},
            ],
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    conversations = response.json()["conversations"]
    assert len(conversations) == 2
    assert {c["evaluation_ai_model_id"] for c in conversations} == {str(assignment.id)}
    assert sorted(c["parameters"]["temperature"] for c in conversations) == [0.2, 0.9]


async def test_create_with_per_entry_titles(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    """Same model twice, distinguished by per-entry title (same-LLM comparison case)."""
    caller = await _caller(db_session, red_teamer_role, email="titledgroup@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)

    response = await _create_group(
        auth_db_client,
        _token(caller),
        scenario.id,
        {
            "name": "same model, two framings",
            "models": [
                {"evaluation_ai_model_id": str(assignment.id), "title": "  Direct ask  "},
                {"evaluation_ai_model_id": str(assignment.id), "title": "Roleplay framing"},
            ],
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    titles = {c["title"] for c in response.json()["conversations"]}
    assert titles == {"Direct ask", "Roleplay framing"}  # stripped, like the single-create endpoint


async def test_create_blank_entry_title_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="blankentrytitle@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)

    response = await _create_group(
        auth_db_client,
        _token(caller),
        scenario.id,
        {
            "name": "x",
            "models": [{"evaluation_ai_model_id": str(assignment.id), "title": "   "}],
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_create_with_shared_scenario(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="sc@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)

    response = await _create_group(
        auth_db_client,
        _token(caller),
        scenario.id,
        {"name": "s", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["scenario_id"] == str(scenario.id)
    assert body["conversations"][0]["scenario_id"] == str(scenario.id)


async def test_create_empty_models_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="empty@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id)

    response = await _create_group(auth_db_client, _token(caller), scenario.id, {"name": "x", "models": []})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert any("models" in error["loc"] for error in response.json()["errors"])


async def test_create_exceeds_max_group_size_returns_422(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    red_teamer_role: Role,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cap is read lazily from settings; patch the schema's call site to a low value.
    settings = _settings()
    settings.max_conversation_group_size = 2
    monkeypatch.setattr("app.core.conversations.schemas.get_settings", lambda: settings)
    caller = await _caller(db_session, red_teamer_role, email="toobig@example.com")
    evaluation = await _evaluation_for(db_session)
    a1 = await _assignment(db_session, evaluation.id)
    a2 = await _assignment(db_session, evaluation.id)
    a3 = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    models = [{"evaluation_ai_model_id": str(a.id)} for a in (a1, a2, a3)]

    response = await _create_group(auth_db_client, _token(caller), scenario.id, {"name": "big", "models": models})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert any("models" in error["loc"] for error in response.json()["errors"])


async def test_create_blank_name_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="blank@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)

    response = await _create_group(
        auth_db_client,
        _token(caller),
        scenario.id,
        {"name": "   ", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert any("name" in error["loc"] for error in response.json()["errors"])


async def test_create_assignment_from_other_evaluation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="cross@example.com")
    evaluation = await _evaluation_for(db_session)
    valid = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    other_eval = await _evaluation_for(db_session)
    foreign = await _assignment(db_session, other_eval.id)

    response = await _create_group(
        auth_db_client,
        _token(caller),
        scenario.id,
        {
            "name": "x",
            "models": [{"evaluation_ai_model_id": str(valid.id)}, {"evaluation_ai_model_id": str(foreign.id)}],
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)
    # The whole batch is rejected — no group persisted.
    assert (await db_session.execute(select(ConversationGroup))).scalars().all() == []


async def test_create_unknown_scenario_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The scenario is the path now, so an id resolving to nothing stops the batch before
    # any model entry is looked at — and persists no group.
    caller = await _caller(db_session, red_teamer_role, email="ghostscen@example.com")

    response = await _create_group(
        auth_db_client,
        _token(caller),
        uuid4(),
        {"name": "x", "models": [{"evaluation_ai_model_id": str(uuid4())}]},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)
    assert (await db_session.execute(select(ConversationGroup))).scalars().all() == []


async def test_create_against_tombstoned_scenario_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Soft-deleting a scenario stays allowed, so the create path is what has to refuse
    # the dead target — and leave no half-built group behind.
    caller = await _caller(db_session, red_teamer_role, email="deadscen@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    scenario.soft_delete(caller.id)
    db_session.add(scenario)
    await db_session.flush()

    response = await _create_group(
        auth_db_client,
        _token(caller),
        scenario.id,
        {
            "name": "x",
            "models": [{"evaluation_ai_model_id": str(assignment.id)}],
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)
    assert (await db_session.execute(select(ConversationGroup))).scalars().all() == []


async def test_create_invisible_private_evaluation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The scenario exists and the caller holds `conversations:create` globally — only
    # visibility is missing, and the object arm resolving the group through the scenario
    # must keep that a 404 rather than announcing the private group with a 403.
    caller = await _caller(db_session, red_teamer_role, email="outsider@example.com")
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)

    response = await _create_group(
        auth_db_client,
        _token(caller),
        scenario.id,
        {"name": "x", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


# --- get / list ---------------------------------------------------------------


async def test_get_returns_group_with_conversations(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="get@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    created = await _create_group(
        auth_db_client, token, scenario.id, {"name": "g", "models": [{"evaluation_ai_model_id": str(assignment.id)}]}
    )
    conversation_group_id = created.json()["id"]

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{conversation_group_id}", headers=_auth(token)
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["id"] == conversation_group_id
    assert len(body["conversations"]) == 1


async def test_get_other_users_group_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="mine@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    created = await _create_group(
        auth_db_client,
        _token(owner),
        scenario.id,
        {"name": "g", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
    )
    conversation_group_id = created.json()["id"]
    intruder = await _caller(db_session, red_teamer_role, email="nosy@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{conversation_group_id}",
        headers=_auth(_token(intruder)),
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_nested_list_returns_only_own_groups(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="a@example.com")
    other = await _caller(db_session, red_teamer_role, email="b@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id)
    for actor in (owner, other):
        assignment = await _assignment(db_session, evaluation.id)
        await _create_group(
            auth_db_client,
            _token(actor),
            scenario.id,
            {"name": "s", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
        )

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups", headers=_auth(_token(owner))
    )

    assert response.status_code == status.HTTP_200_OK
    page = response.json()
    assert page["total"] == 1
    assert page["items"][0]["user_id"] == str(owner.id)


async def test_nested_list_unknown_evaluation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="nolist@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{uuid4()}/conversation-groups", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_flat_list_across_evaluations_and_filter(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="flat@example.com")
    token = _token(caller)
    here = await _evaluation_for(db_session)
    elsewhere = await _evaluation_for(db_session)
    for evaluation in (here, elsewhere):
        assignment = await _assignment(db_session, evaluation.id)
        scenario = await _scenario(db_session, evaluation.id)
        await _create_group(
            auth_db_client,
            token,
            scenario.id,
            {"name": "s", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
        )

    all_groups = await auth_db_client.get("/api/v1/conversation-groups", headers=_auth(token))
    filtered = await auth_db_client.get(f"/api/v1/conversation-groups?evaluation_id={here.id}", headers=_auth(token))

    assert all_groups.json()["total"] == 2
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["evaluation_id"] == str(here.id)


async def test_scenario_filter_narrows_both_group_lists(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The symmetry the conversation lists already had: a group targets exactly one
    # scenario, so both list shapes have to be able to say which.
    caller = await _caller(db_session, red_teamer_role, email="scenfilter@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    wanted = await _scenario(db_session, evaluation.id)
    other = await _scenario(db_session, evaluation.id)
    for scenario in (wanted, other):
        await _create_group(
            auth_db_client,
            token,
            scenario.id,
            {"name": "s", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
        )

    nested = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups?scenario_id={wanted.id}", headers=_auth(token)
    )
    flat = await auth_db_client.get(f"/api/v1/conversation-groups?scenario_id={wanted.id}", headers=_auth(token))
    unfiltered = await auth_db_client.get("/api/v1/conversation-groups", headers=_auth(token))

    assert unfiltered.json()["total"] == 2
    for response in (nested, flat):
        assert response.status_code == status.HTTP_200_OK
        page = response.json()
        assert page["total"] == 1
        assert page["items"][0]["scenario_id"] == str(wanted.id)


async def test_scenario_filter_cannot_widen_the_visibility_scope(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # A filter narrows within the scope, never past it — naming another owner's scenario
    # returns an empty page, not their group.
    owner = await _caller(db_session, red_teamer_role, email="scenfilter-owner@example.com")
    outsider = await _caller(db_session, red_teamer_role, email="scenfilter-outsider@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    await _create_group(
        auth_db_client,
        _token(owner),
        scenario.id,
        {"name": "s", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
    )

    response = await auth_db_client.get(
        f"/api/v1/conversation-groups?scenario_id={scenario.id}", headers=_auth(_token(outsider))
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 0


# --- patch / delete -----------------------------------------------------------


async def test_patch_renames_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="rename@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    created = await _create_group(
        auth_db_client, token, scenario.id, {"name": "old", "models": [{"evaluation_ai_model_id": str(assignment.id)}]}
    )
    conversation_group_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{conversation_group_id}",
        json={"name": "new"},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["name"] == "new"
    assert len(body["conversations"]) == 1  # members still embedded


async def test_patch_blank_name_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="blankpatch@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    created = await _create_group(
        auth_db_client, token, scenario.id, {"name": "old", "models": [{"evaluation_ai_model_id": str(assignment.id)}]}
    )
    conversation_group_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{conversation_group_id}",
        json={"name": " "},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_delete_cascades_to_member_conversations(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="del@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    first = await _assignment(db_session, evaluation.id)
    second = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    created = await _create_group(
        auth_db_client,
        token,
        scenario.id,
        {
            "name": "s",
            "models": [{"evaluation_ai_model_id": str(first.id)}, {"evaluation_ai_model_id": str(second.id)}],
        },
    )
    body = created.json()
    conversation_group_id = body["id"]
    conversation_ids = [c["id"] for c in body["conversations"]]

    deleted = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{conversation_group_id}", headers=_auth(token)
    )
    group_follow_up = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{conversation_group_id}", headers=_auth(token)
    )

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert group_follow_up.status_code == status.HTTP_404_NOT_FOUND
    # Every member conversation is cascade-soft-deleted with the group.
    for conversation_id in conversation_ids:
        follow_up = await auth_db_client.get(
            f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(token)
        )
        assert follow_up.status_code == status.HTTP_404_NOT_FOUND


async def test_manager_reads_other_users_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role, manager_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="owner2@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    created = await _create_group(
        auth_db_client,
        _token(owner),
        scenario.id,
        {"name": "s", "models": [{"evaluation_ai_model_id": str(assignment.id)}]},
    )
    conversation_group_id = created.json()["id"]
    manager = await _caller(db_session, manager_role, email="mgr@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{conversation_group_id}",
        headers=_auth(_token(manager)),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["user_id"] == str(owner.id)


# --- grouping via conversation create -----------------------------------------


async def test_conversation_create_attaches_to_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="attach@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    seed = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    created = await _create_group(
        auth_db_client, token, scenario.id, {"name": "s", "models": [{"evaluation_ai_model_id": str(seed.id)}]}
    )
    conversation_group_id = created.json()["id"]
    extra = await _assignment(db_session, evaluation.id)

    added = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario.id}/conversations",
        json={
            "evaluation_ai_model_id": str(extra.id),
            "conversation_group_id": conversation_group_id,
        },
        headers=_auth(token),
    )
    grouped = await auth_db_client.get(
        f"/api/v1/conversations?conversation_group_id={conversation_group_id}", headers=_auth(token)
    )

    assert added.status_code == status.HTTP_201_CREATED
    assert added.json()["conversation_group_id"] == conversation_group_id
    assert grouped.json()["total"] == 2  # the seed window plus the added one


async def test_conversation_create_with_group_from_other_evaluation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="xsess@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    other_eval = await _evaluation_for(db_session)
    other_assignment = await _assignment(db_session, other_eval.id)
    other_scenario = await _scenario(db_session, other_eval.id)
    foreign_group = await _create_group(
        auth_db_client,
        token,
        other_scenario.id,
        {
            "name": "s",
            "models": [{"evaluation_ai_model_id": str(other_assignment.id)}],
        },
    )
    foreign_group_id = foreign_group.json()["id"]

    response = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario.id}/conversations",
        json={
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": foreign_group_id,
        },
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_conversation_create_requires_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="nosession@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)

    response = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario.id}/conversations",
        # conversation_group_id omitted
        json={"evaluation_ai_model_id": str(assignment.id)},
        headers=_auth(_token(caller)),
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert any("conversation_group_id" in error["loc"] for error in response.json()["errors"])


# --- group size cap (409) ----------------------------------------------------


async def test_conversation_create_into_full_group_returns_409(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    red_teamer_role: Role,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cap is enforced in the service when adding to an existing group.
    settings = _settings()
    settings.max_conversation_group_size = 2
    monkeypatch.setattr("app.core.conversations.services.groups.get_settings", lambda: settings)
    caller = await _caller(db_session, red_teamer_role, email="addfull@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    a1 = await _assignment(db_session, evaluation.id)
    a2 = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    created = await _create_group(
        auth_db_client,
        token,
        scenario.id,
        {
            "name": "full",
            "models": [{"evaluation_ai_model_id": str(a1.id)}, {"evaluation_ai_model_id": str(a2.id)}],
        },
    )
    group_id = created.json()["id"]
    extra = await _assignment(db_session, evaluation.id)

    response = await auth_db_client.post(
        f"/api/v1/scenarios/{scenario.id}/conversations",
        json={
            "evaluation_ai_model_id": str(extra.id),
            "conversation_group_id": group_id,
        },
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    _assert_problem(response, status.HTTP_409_CONFLICT)


async def test_move_into_full_group_returns_409(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    red_teamer_role: Role,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Moving a conversation into a group already at the cap is rejected (409); the
    # locked target row makes the count check race-free.
    settings = _settings()
    settings.max_conversation_group_size = 2
    monkeypatch.setattr("app.core.conversations.services.groups.get_settings", lambda: settings)
    caller = await _caller(db_session, red_teamer_role, email="movefull@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id)
    # Target already at the cap (two conversations).
    t1 = await _assignment(db_session, evaluation.id)
    t2 = await _assignment(db_session, evaluation.id)
    target = await _create_group(
        auth_db_client,
        token,
        scenario.id,
        {
            "name": "tgt",
            "models": [{"evaluation_ai_model_id": str(t1.id)}, {"evaluation_ai_model_id": str(t2.id)}],
        },
    )
    # Source keeps two conversations so the move attempt doesn't first empty it.
    s1 = await _assignment(db_session, evaluation.id)
    s2 = await _assignment(db_session, evaluation.id)
    source = await _create_group(
        auth_db_client,
        token,
        scenario.id,
        {
            "name": "src",
            "models": [{"evaluation_ai_model_id": str(s1.id)}, {"evaluation_ai_model_id": str(s2.id)}],
        },
    )
    moved_id = source.json()["conversations"][0]["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{moved_id}",
        json={"conversation_group_id": target.json()["id"]},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    _assert_problem(response, status.HTTP_409_CONFLICT)


# --- move (PATCH conversation.conversation_group_id) -------------------------------------


async def _two_groups(
    auth_db_client: AsyncClient, db_session: AsyncSession, token: str, evaluation: Evaluation
) -> tuple[str, str, str]:
    """Create a source group with two windows + a separate target group; return ids.

    Returns ``(source_id, target_id, moved_conversation_id)`` — the source keeps a
    sibling so the first move never empties it.
    """
    first = await _assignment(db_session, evaluation.id)
    second = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    source = await _create_group(
        auth_db_client,
        token,
        scenario.id,
        {
            "name": "source",
            "models": [{"evaluation_ai_model_id": str(first.id)}, {"evaluation_ai_model_id": str(second.id)}],
        },
    )
    third = await _assignment(db_session, evaluation.id)
    target = await _create_group(
        auth_db_client, token, scenario.id, {"name": "target", "models": [{"evaluation_ai_model_id": str(third.id)}]}
    )
    moved = source.json()["conversations"][0]["id"]
    return source.json()["id"], target.json()["id"], moved


async def test_move_into_group_of_another_scenario_returns_409(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The scenario link is immutable, so a conversation only moves between groups
    # sharing its scenario — a target of another scenario is a conflict, not a move.
    caller = await _caller(db_session, red_teamer_role, email="movemismatch@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    source_id, _, moved_id = await _two_groups(auth_db_client, db_session, token, evaluation)
    other_scenario = await _scenario(db_session, evaluation.id)
    fourth = await _assignment(db_session, evaluation.id)
    foreign_target = await _create_group(
        auth_db_client,
        token,
        other_scenario.id,
        {
            "name": "other-scenario target",
            "models": [{"evaluation_ai_model_id": str(fourth.id)}],
        },
    )

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{moved_id}",
        json={"conversation_group_id": foreign_target.json()["id"]},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    unmoved = await auth_db_client.get(f"/api/v1/conversations?conversation_group_id={source_id}", headers=_auth(token))
    assert {c["id"] for c in unmoved.json()["items"]} >= {moved_id}


async def test_move_conversation_to_another_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="move@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    source_id, target_id, moved_id = await _two_groups(auth_db_client, db_session, token, evaluation)

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{moved_id}",
        json={"conversation_group_id": target_id},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["conversation_group_id"] == target_id
    # Source still has its other window, so it survives; target now lists the moved one.
    grouped_source = await auth_db_client.get(
        f"/api/v1/conversations?conversation_group_id={source_id}", headers=_auth(token)
    )
    grouped_target = await auth_db_client.get(
        f"/api/v1/conversations?conversation_group_id={target_id}", headers=_auth(token)
    )
    assert grouped_source.json()["total"] == 1
    assert {c["id"] for c in grouped_target.json()["items"]} >= {moved_id}


async def test_move_out_of_last_conversation_prunes_source_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="moveprune@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    # Single-window source group, plus a target.
    only = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    source = await _create_group(
        auth_db_client, token, scenario.id, {"name": "src", "models": [{"evaluation_ai_model_id": str(only.id)}]}
    )
    target_model = await _assignment(db_session, evaluation.id)
    target = await _create_group(
        auth_db_client,
        token,
        scenario.id,
        {"name": "tgt", "models": [{"evaluation_ai_model_id": str(target_model.id)}]},
    )
    source_id = source.json()["id"]
    moved_id = source.json()["conversations"][0]["id"]

    moved = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{moved_id}",
        json={"conversation_group_id": target.json()["id"]},
        headers=_auth(token),
    )
    source_follow_up = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{source_id}", headers=_auth(token)
    )

    assert moved.status_code == status.HTTP_200_OK
    # The emptied source group is pruned, while the moved conversation lives on.
    assert source_follow_up.status_code == status.HTTP_404_NOT_FOUND
    assert await _group_deleted_at(db_session, source_id) is not None


async def test_move_into_group_already_holding_that_model_is_allowed(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # A group may hold several conversations with the same model, so moving a
    # conversation into a group that already has its model is not forbidden.
    caller = await _caller(db_session, red_teamer_role, email="samemodel@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    shared_model = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    # Target group already has a conversation against `shared_model`.
    target = await _create_group(
        auth_db_client,
        token,
        scenario.id,
        {"name": "tgt", "models": [{"evaluation_ai_model_id": str(shared_model.id)}]},
    )
    # Source group has another conversation against the *same* model, plus a
    # sibling so the move doesn't prune the source out from under us.
    sibling = await _assignment(db_session, evaluation.id)
    source = await _create_group(
        auth_db_client,
        token,
        scenario.id,
        {
            "name": "src",
            "models": [{"evaluation_ai_model_id": str(shared_model.id)}, {"evaluation_ai_model_id": str(sibling.id)}],
        },
    )
    target_id = target.json()["id"]
    moved_id = next(
        c["id"] for c in source.json()["conversations"] if c["evaluation_ai_model_id"] == str(shared_model.id)
    )

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{moved_id}",
        json={"conversation_group_id": target_id},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    grouped = await auth_db_client.get(f"/api/v1/conversations?conversation_group_id={target_id}", headers=_auth(token))
    # Target now holds two conversations against the same model.
    models = [c["evaluation_ai_model_id"] for c in grouped.json()["items"]]
    assert models.count(str(shared_model.id)) == 2


async def test_deleting_last_conversation_removes_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="lastconv@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    created = await _create_group(
        auth_db_client, token, scenario.id, {"name": "s", "models": [{"evaluation_ai_model_id": str(assignment.id)}]}
    )
    conversation_group_id = created.json()["id"]
    conversation_id = created.json()["conversations"][0]["id"]

    deleted = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(token)
    )
    group_follow_up = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{conversation_group_id}", headers=_auth(token)
    )

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    # The group had only that conversation, so it is pruned.
    assert group_follow_up.status_code == status.HTTP_404_NOT_FOUND
    assert await _group_deleted_at(db_session, conversation_group_id) is not None


async def test_deleting_one_of_several_conversations_keeps_group(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="keepsession@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    first = await _assignment(db_session, evaluation.id)
    second = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)
    created = await _create_group(
        auth_db_client,
        token,
        scenario.id,
        {
            "name": "s",
            "models": [{"evaluation_ai_model_id": str(first.id)}, {"evaluation_ai_model_id": str(second.id)}],
        },
    )
    conversation_group_id = created.json()["id"]
    conversation_id = created.json()["conversations"][0]["id"]

    deleted = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(token)
    )
    group_follow_up = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversation-groups/{conversation_group_id}", headers=_auth(token)
    )

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    # A sibling remains, so the group lives on with one member.
    assert group_follow_up.status_code == status.HTTP_200_OK
    assert len(group_follow_up.json()["conversations"]) == 1
