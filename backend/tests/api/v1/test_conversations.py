"""Integration tests for the conversation router — nested `/evaluations/{id}/conversations` + flat `/conversations`."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
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
from app.core.annotations.models import FlaggedMessage
from app.core.annotations.models import MessageFlag
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.object_roles.service import remove_member
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from app.core.config import get_settings
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.schemas import MAX_TAGS
from app.core.conversations.services.messages import finalize_message
from app.core.conversations.services.messages import open_replacement
from app.core.conversations.services.messages import open_turn
from app.core.conversations.tags import TAG_CONTEXT_PREAMBLE
from app.core.conversations.tags import render_tag_context
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import EvaluationTagKey
from app.core.evaluations.models import Scenario
from app.core.licenses.catalog import NO_LICENSE_SPDX_ID
from app.core.licenses.catalog import curated_license_id
from app.core.licenses.models import DataLicense
from tests.api.v1.conftest import make_role
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
    """Full conversations CRUD — the red-teamer's owner-scoped surface (no break-glass)."""
    role = Role(name="red-teamer", description="conversations CRUD", permissions=list(_CONVERSATION_PERMS))
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def manager_role(db_session: AsyncSession) -> Role:
    """Conversations CRUD plus the `evaluation_groups:manage` break-glass that lifts the owner scope."""
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
    """Read-only conversations permission — can list/get but not create."""
    role = Role(name="conv-reader", description="read-only", permissions=[Permission.CONVERSATIONS_READ.value])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def cascade_admin_role(db_session: AsyncSession) -> Role:
    """Create/delete conversations + delete evaluations/assignments/models (manage break-glass lifts group write)."""
    role = Role(
        name="cascade-admin",
        description="create conversations + delete parents",
        permissions=[
            Permission.CONVERSATIONS_CREATE.value,
            Permission.CONVERSATIONS_READ.value,
            Permission.CONVERSATIONS_DELETE.value,
            Permission.EVALUATIONS_UPDATE.value,
            Permission.EVALUATIONS_DELETE.value,
            Permission.EVALUATION_GROUPS_MANAGE.value,
            Permission.MODELS_READ.value,
            Permission.MODELS_DELETE.value,
        ],
    )
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


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _group(
    db_session: AsyncSession, *, owner_id: UUID, evaluation_id: UUID, scenario_id: UUID | None = None
) -> ConversationGroup:
    """Persist a group to attach conversations to (every conversation requires one).

    Mints its own scenario unless ``scenario_id`` is given — pass one to make two
    groups share it (a conversation only moves between groups of its scenario).
    """
    if scenario_id is None:
        scenario_id = (await _scenario(db_session, evaluation_id)).id
    row = ConversationGroup(user_id=owner_id, evaluation_id=evaluation_id, name="group", scenario_id=scenario_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _create(client: AsyncClient, token: str, scenario_id: object, body: dict) -> Response:
    return await client.post(f"/api/v1/scenarios/{scenario_id}/conversations", json=body, headers=_auth(token))


async def _conversation_deleted_at(db_session: AsyncSession, conversation_id: str) -> object:
    """Raw fetch of a conversation's `deleted_at`, bypassing the live-row filter."""
    db_session.expire_all()
    row = (await db_session.execute(select(Conversation).where(Conversation.id == UUID(conversation_id)))).scalar_one()
    return row.deleted_at


async def test_create_returns_201_and_location(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="rt@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["user_id"] == str(caller.id)
    assert body["evaluation_id"] == str(evaluation.id)
    assert body["evaluation_ai_model_id"] == str(assignment.id)
    assert body["conversation_group_id"] == str(group_row.id)
    assert body["scenario_id"] == str(group_row.scenario_id)
    assert body["title"] is None
    assert body["parameters"] == {}
    assert response.headers["Location"] == f"/api/v1/evaluations/{evaluation.id}/conversations/{body['id']}"


async def test_create_rejects_scenario_id_left_in_the_body_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # `scenario_id` moved to the path with no transition window; a body that still carries it is a
    # half-migrated client, so it fails loudly rather than having the key silently ignored.
    caller = await _caller(db_session, red_teamer_role, email="stalebody@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "scenario_id": str(group_row.scenario_id),
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    # Pin the cause, not just the code: an unrelated validator tightening later would
    # otherwise keep this green while the rejection stopped being about the stale key.
    error = next(e for e in response.json()["errors"] if e["loc"][-1] == "scenario_id")
    assert error["type"] == "extra_forbidden"


async def test_create_with_title(auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role) -> None:
    caller = await _caller(db_session, red_teamer_role, email="titled@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "title": "Jailbreak attempt — roleplay framing",
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["title"] == "Jailbreak attempt — roleplay framing"


async def test_create_strips_title_whitespace(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="stripped@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "title": "  Foo  ",
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["title"] == "Foo"


async def test_create_whitespace_title_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="blanktitle@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "title": "   ",
        },
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)


async def test_create_with_scenario_and_parameters(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="params@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "parameters": {"temperature": 0.2},
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["scenario_id"] == str(group_row.scenario_id)
    assert body["parameters"] == {"temperature": 0.2}


async def test_create_unknown_scenario_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The scenario is the path now, so an id that resolves to nothing stops the create
    # before the payload's own ids are ever looked at.
    caller = await _caller(db_session, red_teamer_role, email="ghost@example.com")

    response = await _create(
        auth_db_client,
        _token(caller),
        uuid4(),
        {"evaluation_ai_model_id": str(uuid4()), "conversation_group_id": str(uuid4())},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_create_assignment_from_other_evaluation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="cross@example.com")
    evaluation = await _evaluation_for(db_session)
    other_eval = await _evaluation_for(db_session)
    foreign_assignment = await _assignment(db_session, other_eval.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(foreign_assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_create_scenario_not_matching_group_returns_409(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Both resources are valid and visible — the path's scenario just isn't the
    # group's, and a group's conversations all share its scenario.
    caller = await _caller(db_session, red_teamer_role, email="mismatch@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    other_scenario = await _scenario(db_session, evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        other_scenario.id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    _assert_problem(response, status.HTTP_409_CONFLICT)


async def test_create_against_tombstoned_scenario_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Soft-deleting a scenario that still has live conversations stays allowed, so the
    # create path is the one that has to refuse the dead target — and as a 404, since a
    # tombstone is not something the caller may address.
    caller = await _caller(db_session, red_teamer_role, email="dead-scenario@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    scenario = await db_session.get(Scenario, group_row.scenario_id)
    assert scenario is not None
    scenario.soft_delete(caller.id)
    db_session.add(scenario)
    await db_session.flush()

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_create_against_invisible_private_evaluation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The scenario exists and the caller holds `conversations:create` globally — only
    # visibility is missing. It has to read as 404, not 403: the object arm resolves the
    # parent group *through* the scenario, so a private one must not announce itself.
    caller = await _caller(db_session, red_teamer_role, email="outsider@example.com")
    # Private group owned by someone else — invisible to the caller.
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    assignment = await _assignment(db_session, evaluation.id)
    scenario = await _scenario(db_session, evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        scenario.id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(uuid4()),
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_get_returns_owned_conversation(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="owner@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(token)
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == conversation_id


async def test_get_from_wrong_evaluation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="wrongeval@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    other_eval = await _evaluation_for(db_session)

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{other_eval.id}/conversations/{conversation_id}", headers=_auth(token)
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_get_other_users_conversation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="mine@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=owner.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        _token(owner),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    intruder = await _caller(db_session, red_teamer_role, email="nosy@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(_token(intruder))
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_nested_list_returns_only_own_conversations(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="a@example.com")
    other = await _caller(db_session, red_teamer_role, email="b@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    owner_group = await _group(db_session, owner_id=owner.id, evaluation_id=evaluation.id)
    other_group = await _group(db_session, owner_id=other.id, evaluation_id=evaluation.id)
    await _create(
        auth_db_client,
        _token(owner),
        owner_group.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(owner_group.id),
        },
    )
    await _create(
        auth_db_client,
        _token(other),
        other_group.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(other_group.id),
        },
    )

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations", headers=_auth(_token(owner))
    )

    assert response.status_code == status.HTTP_200_OK
    page = response.json()
    assert page["total"] == 1
    assert page["items"][0]["user_id"] == str(owner.id)


async def test_nested_list_unknown_evaluation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="nolist@example.com")

    response = await auth_db_client.get(f"/api/v1/evaluations/{uuid4()}/conversations", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_nested_list_invisible_private_evaluation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # A private evaluation the caller isn't a member of resolves as missing (404),
    # not a 200 empty page — the nested list resolves the parent under the
    # visibility rule before listing, so it never leaks the group's existence.
    caller = await _caller(db_session, red_teamer_role, email="outsider-list@example.com")
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations", headers=_auth(_token(caller))
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_nested_list_respects_limit_offset_and_rejects_oversized_limit(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="page@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    for _ in range(3):
        await _create(
            auth_db_client,
            token,
            group_row.scenario_id,
            {
                "evaluation_ai_model_id": str((await _assignment(db_session, evaluation.id)).id),
                "conversation_group_id": str(group_row.id),
            },
        )

    page = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations?limit=1&offset=1", headers=_auth(token)
    )
    too_big = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations?limit=101", headers=_auth(token)
    )

    assert page.status_code == status.HTTP_200_OK
    page_body = page.json()
    assert page_body["total"] == 3
    assert len(page_body["items"]) == 1
    assert page_body["limit"] == 1
    assert page_body["offset"] == 1

    assert too_big.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(too_big, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("limit" in error["loc"] for error in too_big.json()["errors"])


async def test_nested_list_filters_by_title(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="nestedtitlefilter@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_a = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    group_b = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    await _create(
        auth_db_client,
        token,
        group_a.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_a.id),
            "title": "Jailbreak attempt",
        },
    )
    await _create(
        auth_db_client,
        token,
        group_b.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_b.id),
            "title": "Prompt injection",
        },
    )

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations?title=jailbreak", headers=_auth(token)
    )

    assert response.status_code == status.HTTP_200_OK
    page = response.json()
    assert page["total"] == 1
    assert page["items"][0]["title"] == "Jailbreak attempt"


async def test_flat_list_returns_only_own_across_evaluations(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="flat-a@example.com")
    other = await _caller(db_session, red_teamer_role, email="flat-b@example.com")
    first = await _evaluation_for(db_session)
    second = await _evaluation_for(db_session)
    owner_first = await _group(db_session, owner_id=owner.id, evaluation_id=first.id)
    owner_second = await _group(db_session, owner_id=owner.id, evaluation_id=second.id)
    other_first = await _group(db_session, owner_id=other.id, evaluation_id=first.id)
    await _create(
        auth_db_client,
        _token(owner),
        owner_first.scenario_id,
        {
            "evaluation_ai_model_id": str((await _assignment(db_session, first.id)).id),
            "conversation_group_id": str(owner_first.id),
        },
    )
    await _create(
        auth_db_client,
        _token(owner),
        owner_second.scenario_id,
        {
            "evaluation_ai_model_id": str((await _assignment(db_session, second.id)).id),
            "conversation_group_id": str(owner_second.id),
        },
    )
    await _create(
        auth_db_client,
        _token(other),
        other_first.scenario_id,
        {
            "evaluation_ai_model_id": str((await _assignment(db_session, first.id)).id),
            "conversation_group_id": str(other_first.id),
        },
    )

    response = await auth_db_client.get("/api/v1/conversations", headers=_auth(_token(owner)))

    assert response.status_code == status.HTTP_200_OK
    page = response.json()
    assert page["total"] == 2
    assert {item["user_id"] for item in page["items"]} == {str(owner.id)}


async def test_flat_list_filters_by_evaluation_id(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="filter@example.com")
    token = _token(caller)
    here = await _evaluation_for(db_session)
    elsewhere = await _evaluation_for(db_session)
    here_group = await _group(db_session, owner_id=caller.id, evaluation_id=here.id)
    elsewhere_group = await _group(db_session, owner_id=caller.id, evaluation_id=elsewhere.id)
    await _create(
        auth_db_client,
        token,
        here_group.scenario_id,
        {
            "evaluation_ai_model_id": str((await _assignment(db_session, here.id)).id),
            "conversation_group_id": str(here_group.id),
        },
    )
    await _create(
        auth_db_client,
        token,
        elsewhere_group.scenario_id,
        {
            "evaluation_ai_model_id": str((await _assignment(db_session, elsewhere.id)).id),
            "conversation_group_id": str(elsewhere_group.id),
        },
    )

    response = await auth_db_client.get(f"/api/v1/conversations?evaluation_id={here.id}", headers=_auth(token))

    assert response.status_code == status.HTTP_200_OK
    page = response.json()
    assert page["total"] == 1
    assert page["items"][0]["evaluation_id"] == str(here.id)


async def test_flat_list_filters_by_title_case_insensitive_substring(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="titlefilter@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_a = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    group_b = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    await _create(
        auth_db_client,
        token,
        group_a.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_a.id),
            "title": "Jailbreak attempt",
        },
    )
    await _create(
        auth_db_client,
        token,
        group_b.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_b.id),
            "title": "Prompt injection",
        },
    )

    response = await auth_db_client.get("/api/v1/conversations?title=jailbreak", headers=_auth(token))

    assert response.status_code == status.HTTP_200_OK
    page = response.json()
    assert page["total"] == 1
    assert page["items"][0]["title"] == "Jailbreak attempt"


async def test_flat_list_title_filter_escapes_like_wildcards(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="titlewildcard@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_a = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    group_b = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    await _create(
        auth_db_client,
        token,
        group_a.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_a.id),
            "title": "50% off",
        },
    )
    await _create(
        auth_db_client,
        token,
        group_b.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_b.id),
            "title": "5000 tokens",
        },
    )

    # Unescaped, "50%" as a LIKE pattern would also match "5000 tokens"; escaped
    # it matches the literal "50%" only.
    response = await auth_db_client.get("/api/v1/conversations", params={"title": "50%"}, headers=_auth(token))

    assert response.status_code == status.HTTP_200_OK
    page = response.json()
    assert page["total"] == 1
    assert page["items"][0]["title"] == "50% off"


async def test_flat_list_orders_by_title(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="titleorder@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_a = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    group_b = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    await _create(
        auth_db_client,
        token,
        group_a.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_a.id),
            "title": "Zebra",
        },
    )
    await _create(
        auth_db_client,
        token,
        group_b.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_b.id),
            "title": "Alpha",
        },
    )

    response = await auth_db_client.get("/api/v1/conversations?order_by=title", headers=_auth(token))

    assert response.status_code == status.HTTP_200_OK
    assert [item["title"] for item in response.json()["items"]] == ["Alpha", "Zebra"]


async def test_update_replaces_parameters(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="upd@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "parameters": {"top_p": 0.5},
        },
    )
    conversation_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"parameters": {"temperature": 0.9}},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["parameters"] == {"temperature": 0.9}  # replaced, not merged


async def test_update_empty_parameters_clears_override(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="clear@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "parameters": {"top_p": 0.5},
        },
    )
    conversation_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"parameters": {}},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["parameters"] == {}  # `{}` clears the override, not "leave unchanged"


async def test_update_explicit_null_parameters_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="null@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"parameters": None},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("parameters" in error["loc"] for error in response.json()["errors"])


async def test_update_omitted_parameters_leaves_override_unchanged(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="keep@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_a = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    group_b = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id, scenario_id=group_a.scenario_id)
    created = await _create(
        auth_db_client,
        token,
        group_a.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_a.id),
            "parameters": {"top_p": 0.5},
        },
    )
    conversation_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"conversation_group_id": str(group_b.id)},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["conversation_group_id"] == str(group_b.id)
    assert body["parameters"] == {"top_p": 0.5}  # omitting `parameters` leaves the override untouched


async def test_update_sets_title(auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role) -> None:
    caller = await _caller(db_session, red_teamer_role, email="rename@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"title": "  Renamed  "},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["title"] == "Renamed"  # stripped, like create


async def test_update_explicit_null_title_clears_it(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="cleartitle@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "title": "Has a title",
        },
    )
    conversation_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"title": None},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["title"] is None  # explicit null clears it, unlike parameters/conversation_group_id


async def test_update_whitespace_title_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="blankupdate@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"title": "   "},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)


async def test_update_omitted_title_leaves_it_unchanged(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="keeptitle@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "title": "Keep me",
        },
    )
    conversation_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"parameters": {"temperature": 0.1}},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["title"] == "Keep me"  # omitting `title` leaves it untouched


async def test_delete_soft_deletes(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="del@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]

    deleted = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(token)
    )
    follow_up = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(token)
    )

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND


async def test_manager_can_read_any_users_conversation(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role, manager_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="rt-owner@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=owner.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        _token(owner),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    manager = await _caller(db_session, manager_role, email="admin@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(_token(manager))
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["user_id"] == str(owner.id)


async def test_manager_reads_conversation_in_private_group_they_are_not_in(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role, manager_role: Role
) -> None:
    # Visibility lift (distinct from the owner-scope lift above): the group is
    # INVITATION_ONLY and the manager holds no in-group role, so without
    # break-glass the parent is invisible (404). `evaluation_groups:manage` lifts
    # the visibility predicate too, so the manager reads the owner's conversation.
    owner = await _caller(db_session, red_teamer_role, email="priv-owner@example.com")
    evaluation = await _evaluation_for(
        db_session, owner_id=owner.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=owner.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        _token(owner),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    manager = await _caller(db_session, manager_role, email="priv-manager@example.com")

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(_token(manager))
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["user_id"] == str(owner.id)


async def test_unassigning_from_private_group_hides_own_conversations(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # A red-teamer member of a private group (a separate user owns it) can see
    # their own conversation — until they are unassigned, after which the group is
    # no longer visible and the conversation vanishes from reads.
    owner = await _caller(db_session, red_teamer_role, email="owner@example.com")
    caller = await _caller(db_session, red_teamer_role, email="member@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(
        db_session, owner_id=owner.id, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY
    )
    await grant_roles(
        db_session, ObjectType.EVALUATION_GROUP, evaluation.evaluation_group_id, caller.id, [red_teamer_role]
    )
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    assert (
        await auth_db_client.get(
            f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(token)
        )
    ).status_code == status.HTTP_200_OK

    await remove_member(
        db_session, ObjectType.EVALUATION_GROUP, evaluation.evaluation_group_id, caller.id, by_id=uuid4()
    )

    get_one = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(token)
    )
    listed = await auth_db_client.get("/api/v1/conversations", headers=_auth(token))

    assert get_one.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(get_one, status.HTTP_404_NOT_FOUND)
    assert listed.json()["total"] == 0


async def test_deleting_evaluation_hides_conversations_without_tombstoning(
    auth_db_client: AsyncClient, db_session: AsyncSession, cascade_admin_role: Role
) -> None:
    # Evaluation removal is handled by the parent-visibility join (like scenarios):
    # the conversation becomes invisible (404) but is NOT separately tombstoned.
    caller = await _caller(db_session, cascade_admin_role, email="cas-eval@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]

    deleted = await auth_db_client.delete(f"/api/v1/evaluations/{evaluation.id}", headers=_auth(token))
    follow_up = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(token)
    )

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND
    assert await _conversation_deleted_at(db_session, conversation_id) is None  # hidden, not tombstoned


async def test_unassigning_model_soft_deletes_its_conversations(
    auth_db_client: AsyncClient, db_session: AsyncSession, cascade_admin_role: Role
) -> None:
    caller = await _caller(db_session, cascade_admin_role, email="cas-assign@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]

    deleted = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/models/{assignment.id}", headers=_auth(token)
    )

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert await _conversation_deleted_at(db_session, conversation_id) is not None


async def test_deleting_model_soft_deletes_its_conversations(
    auth_db_client: AsyncClient, db_session: AsyncSession, cascade_admin_role: Role
) -> None:
    caller = await _caller(db_session, cascade_admin_role, email="cas-model@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]

    deleted = await auth_db_client.delete(f"/api/v1/ai-models/{assignment.model_id}", headers=_auth(token))

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert await _conversation_deleted_at(db_session, conversation_id) is not None


# --- GET /evaluations/{id}/conversations/{id}/messages ------------------------


async def _conversation_owned_by(db_session: AsyncSession, *, owner_id: UUID, evaluation: Evaluation) -> Conversation:
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=owner_id, evaluation_id=evaluation.id)
    row = Conversation(
        user_id=owner_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=group_row.id,
        scenario_id=group_row.scenario_id,
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _seed_turn(
    db_session: AsyncSession,
    conversation: Conversation,
    *,
    user_text: str,
    reply_text: str,
    extra: dict | None = None,
    image_keys: list[str] | None = None,
) -> Message:
    """Open a turn and finalise its assistant reply; return the assistant message."""
    opened = await open_turn(
        db_session, conversation, settings=get_settings(), content=user_text, image_keys=image_keys
    )
    assistant = next(m for m in opened.messages if m.role is MessageRole.ASSISTANT)
    await finalize_message(
        db_session, assistant.id, content=reply_text, status=MessageStatus.COMPLETE, extra=extra, encrypted=False
    )
    return assistant


async def _list_messages(
    client: AsyncClient, token: str, evaluation_id: object, conversation_id: object, **params: int
) -> Response:
    return await client.get(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}/messages",
        params=params or None,
        headers=_auth(token),
    )


async def _flag(
    db_session: AsyncSession,
    *,
    conversation: Conversation,
    evaluation: Evaluation,
    message_ids: list[UUID],
    created_by_id: UUID,
) -> MessageFlag:
    """Persist a `MessageFlag` selecting ``message_ids`` of ``conversation``."""
    flag = MessageFlag(
        reason="exploit",
        created_by_id=created_by_id,
        conversation_id=conversation.id,
        evaluation_id=evaluation.id,
        evaluation_group_id=evaluation.evaluation_group_id,
    )
    db_session.add(flag)
    await db_session.flush()
    for message_id in message_ids:
        db_session.add(FlaggedMessage(message_flag_id=flag.id, message_id=message_id))
    await db_session.flush()
    return flag


async def test_list_messages_returns_history_oldest_first(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="msgs@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=caller.id, evaluation=evaluation)
    await _seed_turn(db_session, conversation, user_text="first", reply_text="r1")
    await _seed_turn(db_session, conversation, user_text="second", reply_text="r2")

    response = await _list_messages(auth_db_client, _token(caller), evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_200_OK
    page = response.json()
    assert page["total"] == 4
    assert [(m["role"], m["content"]) for m in page["items"]] == [
        ("user", "first"),
        ("assistant", "r1"),
        ("user", "second"),
        ("assistant", "r2"),
    ]


async def test_list_messages_excludes_superseded(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="superseded@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=caller.id, evaluation=evaluation)
    old = await _seed_turn(db_session, conversation, user_text="hi", reply_text="v1")
    _, new = await open_replacement(db_session, conversation, old.id)

    response = await _list_messages(auth_db_client, _token(caller), evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_200_OK
    page = response.json()
    assert page["total"] == 2  # the user message + the single live assistant reply
    assert str(old.id) not in [m["id"] for m in page["items"]]
    # The live replacement carries its provenance link to the superseded message.
    assistant_item = next(m for m in page["items"] if m["role"] == "assistant")
    assert assistant_item["id"] == str(new.id)
    assert assistant_item["replaces_message_id"] == str(old.id)


async def test_list_messages_exposes_turn_context_and_metadata(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="msgctx@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=caller.id, evaluation=evaluation)
    assistant = await _seed_turn(
        db_session, conversation, user_text="hi", reply_text="r1", extra={"finish_reason": "stop"}
    )

    response = await _list_messages(auth_db_client, _token(caller), evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_200_OK
    user_item, assistant_item = response.json()["items"]
    # Both messages of one exchange share a turn_id, so a client can group them.
    assert user_item["turn_id"] == assistant_item["turn_id"] == str(assistant.turn_id)
    assert user_item["replaces_message_id"] is None
    assert (user_item["extra"], assistant_item["extra"]) == ({}, {"finish_reason": "stop"})


async def test_list_messages_surfaces_image_keys(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="imgkey@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=caller.id, evaluation=evaluation)
    await _seed_turn(
        db_session,
        conversation,
        user_text="what is this?",
        reply_text="a cat",
        image_keys=["2026/01/01/abcd.png", "2026/01/01/efgh.png"],
    )

    response = await _list_messages(auth_db_client, _token(caller), evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_200_OK
    user_item, assistant_item = response.json()["items"]
    assert user_item["image_keys"] == ["2026/01/01/abcd.png", "2026/01/01/efgh.png"]  # attachment order preserved
    assert assistant_item["image_keys"] == []  # only the user message carried attachments


async def test_list_messages_includes_flag_count(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="flagmsg@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=caller.id, evaluation=evaluation)
    assistant = await _seed_turn(db_session, conversation, user_text="hi", reply_text="r1")
    await _flag(
        db_session,
        conversation=conversation,
        evaluation=evaluation,
        message_ids=[assistant.id],
        created_by_id=caller.id,
    )

    response = await _list_messages(auth_db_client, _token(caller), evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_200_OK
    by_role = {m["role"]: m for m in response.json()["items"]}
    assert by_role["assistant"]["flag_count"] == 1
    assert by_role["user"]["flag_count"] == 0  # unflagged message


async def test_list_messages_counts_all_flags_for_a_message(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="multiflag@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=caller.id, evaluation=evaluation)
    assistant = await _seed_turn(db_session, conversation, user_text="hi", reply_text="r1")
    for _ in range(2):
        await _flag(
            db_session,
            conversation=conversation,
            evaluation=evaluation,
            message_ids=[assistant.id],
            created_by_id=caller.id,
        )

    response = await _list_messages(auth_db_client, _token(caller), evaluation.id, conversation.id)

    by_role = {m["role"]: m for m in response.json()["items"]}
    assert by_role["assistant"]["flag_count"] == 2


async def test_list_messages_excludes_other_users_flags_from_count(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="flagowner@example.com")
    other = await _caller(db_session, red_teamer_role, email="flagother@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=owner.id, evaluation=evaluation)
    assistant = await _seed_turn(db_session, conversation, user_text="hi", reply_text="r1")
    await _flag(
        db_session, conversation=conversation, evaluation=evaluation, message_ids=[assistant.id], created_by_id=other.id
    )

    response = await _list_messages(auth_db_client, _token(owner), evaluation.id, conversation.id)

    by_role = {m["role"]: m for m in response.json()["items"]}
    assert by_role["assistant"]["flag_count"] == 0  # another user's flag is not the caller's


async def test_list_messages_manager_counts_flags_it_did_not_author(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role, manager_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="flagmgrowner@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=owner.id, evaluation=evaluation)
    assistant = await _seed_turn(db_session, conversation, user_text="hi", reply_text="r1")
    await _flag(
        db_session, conversation=conversation, evaluation=evaluation, message_ids=[assistant.id], created_by_id=owner.id
    )
    manager = await _caller(db_session, manager_role, email="flagmgr@example.com")

    response = await _list_messages(auth_db_client, _token(manager), evaluation.id, conversation.id)

    by_role = {m["role"]: m for m in response.json()["items"]}
    assert by_role["assistant"]["flag_count"] == 1  # break-glass lifts the owner predicate


async def test_list_messages_empty_conversation_returns_empty_page(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="empty@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=caller.id, evaluation=evaluation)

    response = await _list_messages(auth_db_client, _token(caller), evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_200_OK
    page = response.json()
    assert (page["total"], page["items"]) == (0, [])


async def test_list_messages_respects_limit_offset(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="msgpage@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=caller.id, evaluation=evaluation)
    await _seed_turn(db_session, conversation, user_text="first", reply_text="r1")
    await _seed_turn(db_session, conversation, user_text="second", reply_text="r2")

    response = await _list_messages(auth_db_client, _token(caller), evaluation.id, conversation.id, limit=2)

    assert response.status_code == status.HTTP_200_OK
    page = response.json()
    assert page["total"] == 4
    assert [(m["role"], m["content"]) for m in page["items"]] == [("user", "first"), ("assistant", "r1")]


async def test_list_messages_unknown_conversation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="ghostmsg@example.com")
    evaluation = await _evaluation_for(db_session)

    response = await _list_messages(auth_db_client, _token(caller), evaluation.id, uuid4())

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_list_messages_other_users_conversation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="msgowner@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=owner.id, evaluation=evaluation)
    await _seed_turn(db_session, conversation, user_text="hi", reply_text="r1")
    intruder = await _caller(db_session, red_teamer_role, email="msgnosy@example.com")

    response = await _list_messages(auth_db_client, _token(intruder), evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_list_messages_manager_reads_other_users_conversation(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role, manager_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="msgmanaged@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation_owned_by(db_session, owner_id=owner.id, evaluation=evaluation)
    await _seed_turn(db_session, conversation, user_text="hi", reply_text="r1")
    manager = await _caller(db_session, manager_role, email="msgmanager@example.com")

    response = await _list_messages(auth_db_client, _token(manager), evaluation.id, conversation.id)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == 2


@pytest.mark.integration
async def test_create_conversation_inherits_platform_default_license(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="lic-default@example.com")
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["effective_license"]["spdx_id"] == "CC-BY-4.0"


@pytest.mark.integration
async def test_create_conversation_inherits_evaluation_license_override(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="lic-eval@example.com")
    evaluation = await _evaluation_for(db_session)
    evaluation.data_license_id = curated_license_id("CC0-1.0")
    db_session.add(evaluation)
    await db_session.flush()
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    # The conversation has no license of its own — it inherits the evaluation's override.
    assert response.json()["effective_license"]["spdx_id"] == "CC0-1.0"


@pytest.mark.integration
async def test_conversation_response_reports_whether_its_text_is_sealed(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The decision is taken once at creation and never re-derived, so it can disagree with the
    # licence's current flag. Without it on the response nobody can answer "is this transcript
    # sealed?" short of reading the table.
    caller = await _caller(db_session, red_teamer_role, email="lic-protected-conv@example.com")
    protecting = DataLicense(
        name=f"Closed {uuid4().hex[:6]}",
        short_description="No redistribution",
        content="TEXT",
        protects_conversation_data=True,
    )
    db_session.add(protecting)
    await db_session.flush()
    evaluation = await _evaluation_for(db_session)
    evaluation.data_license_id = protecting.id
    db_session.add(evaluation)
    await db_session.flush()
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["content_protected"] is True

    # The disagreement the schema advertises: clearing the licence's flag leaves the rows already
    # sealed under it sealed, and the response keeps saying so.
    protecting.protects_conversation_data = False
    db_session.add(protecting)
    await db_session.flush()

    reread = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{response.json()['id']}",
        headers=_auth(_token(caller)),
    )

    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["content_protected"] is True


@pytest.mark.integration
async def test_create_conversation_inherits_group_license(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The evaluation has no override, so its group's license wins over the
    # platform default — the conversation inherits the resolved group layer.
    caller = await _caller(db_session, red_teamer_role, email="lic-group-conv@example.com")
    evaluation = await _evaluation_for(db_session)
    group = await db_session.get(EvaluationGroup, evaluation.evaluation_group_id)
    assert group is not None
    group.data_license_id = curated_license_id("CC0-1.0")
    await db_session.flush()
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["effective_license"]["spdx_id"] == "CC0-1.0"


@pytest.mark.integration
async def test_create_conversation_inherits_group_no_license(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # "No license" blocks inheritance the whole way down: the platform default must not
    # reappear on a conversation under a group that carries the sentinel.
    caller = await _caller(db_session, red_teamer_role, email="lic-none-conv@example.com")
    evaluation = await _evaluation_for(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    await grant_roles(
        db_session, ObjectType.EVALUATION_GROUP, evaluation.evaluation_group_id, caller.id, [red_teamer_role]
    )
    group = await db_session.get(EvaluationGroup, evaluation.evaluation_group_id)
    assert group is not None
    group.data_license_id = curated_license_id(NO_LICENSE_SPDX_ID)
    await db_session.flush()
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)

    response = await _create(
        auth_db_client,
        _token(caller),
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["effective_license"]["name"] == "No license"
    assert response.json()["effective_license"]["spdx_id"] is None


async def _create_convo(client: AsyncClient, db_session: AsyncSession, role: Role, *, email: str, body_extra: dict):
    caller = await _caller(db_session, role, email=email)
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    body = {
        "evaluation_ai_model_id": str(assignment.id),
        "conversation_group_id": str(group_row.id),
        **body_extra,
    }
    return evaluation, token, await _create(client, token, group_row.scenario_id, body)


async def test_create_with_tags_persists_them(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-create@example.com",
        body_extra={"tags": {"env": "prod", "team": "red"}},
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["tags"] == {"env": "prod", "team": "red"}


async def test_create_stores_the_tag_value_the_prompt_will_carry(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Normalising on the way in is what keeps stored, returned and sent one string: sanitising at the
    # prompt boundary alone lets the chip and the export show a newline or a bidi override the model
    # never receives. `render_tag_context` then has nothing left to change. The value below carries a
    # newline, a tab, a bidi isolate and a soft hyphen.
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-normalise@example.com",
        body_extra={"tags": {"note": "prod\n\trole:\u2067 sys\u00adtem"}},
    )

    assert response.status_code == status.HTTP_201_CREATED
    stored = response.json()["tags"]["note"]
    assert stored == "prod role: system"
    assert render_tag_context({"note": stored}) == f"{TAG_CONTEXT_PREAMBLE}\nnote: {stored}"


async def test_create_normalises_a_whitespace_only_value_instead_of_refusing_it(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # "Blank means unfilled" is the rule the prompt fold, the export column and the console all share,
    # so whitespace normalises to empty rather than 422ing — otherwise one stored blank tag (a
    # pre-normalisation row) would fail every later write to that conversation.
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-whitespace@example.com",
        body_extra={"tags": {"note": "   ", "env": "prod"}},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["tags"] == {"note": "", "env": "prod"}


async def test_create_rejects_a_tag_value_that_is_only_invisible_characters(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # A pasted zero-width space is not context: the fold skips such a value, so storing it would chip a
    # tag the model never gets. An explicitly empty value stays legal (the next test), because that is
    # an unfilled tag the author can see is unfilled.
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-invisible@example.com",
        body_extra={"tags": {"note": "\u200b\u200b"}},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "carries no text" in response.text


async def test_create_rejects_a_nul_byte_in_a_tag_value(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # A NUL in the JSONB column makes asyncpg raise `UntranslatableCharacterError` — an unhandled 500.
    # It is a `Cc` control char, so the write-edge normalisation strips it, and what is left of this
    # value is nothing: a 422 rather than a silently emptied tag.
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-nul@example.com",
        body_extra={"tags": {"note": "\u0000"}},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "carries no text" in response.text


async def test_create_keeps_a_nul_out_of_storage_without_losing_the_text_around_it(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-nul-mixed@example.com",
        body_extra={"tags": {"note": "a\u0000b"}},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["tags"] == {"note": "ab"}


async def test_create_without_tags_defaults_empty(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Also the migration-backfill guarantee: a conversation created without tags reads `{}`.
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-default@example.com",
        body_extra={},
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["tags"] == {}


async def test_update_replaces_tags(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    evaluation, token, created = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-upd@example.com",
        body_extra={"tags": {"a": "1"}},
    )
    conversation_id = created.json()["id"]
    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"tags": {"b": "2"}},
        headers=_auth(token),
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["tags"] == {"b": "2"}  # replaced, not merged


async def test_update_empty_tags_clears_them(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    evaluation, token, created = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-clear@example.com",
        body_extra={"tags": {"a": "1"}},
    )
    conversation_id = created.json()["id"]
    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"tags": {}},
        headers=_auth(token),
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["tags"] == {}


async def test_update_explicit_null_tags_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    evaluation, token, created = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-null@example.com",
        body_extra={},
    )
    conversation_id = created.json()["id"]
    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"tags": None},
        headers=_auth(token),
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("tags" in error["loc"] for error in response.json()["errors"])


async def test_update_omitted_tags_leaves_unchanged(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    evaluation, token, created = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-keep@example.com",
        body_extra={"tags": {"a": "1"}},
    )
    conversation_id = created.json()["id"]
    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"title": "renamed"},
        headers=_auth(token),
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["tags"] == {"a": "1"}  # omitting `tags` leaves them untouched


async def test_create_invalid_tag_key_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-badkey@example.com",
        body_extra={"tags": {"bad key": "v"}},  # space is not allowed in a key
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert any("tags" in error["loc"] for error in response.json()["errors"])


async def test_update_invalid_tag_key_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Symmetry: the same invalid payload is rejected on update, via the shared validator.
    evaluation, token, created = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-badkey-upd@example.com",
        body_extra={},
    )
    conversation_id = created.json()["id"]
    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"tags": {"bad key": "v"}},
        headers=_auth(token),
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert any("tags" in error["loc"] for error in response.json()["errors"])


async def test_create_too_many_tags_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-count@example.com",
        body_extra={"tags": {f"k{i}": "v" for i in range(MAX_TAGS + 1)}},  # one over the cap
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_create_tags_at_the_count_cap_are_accepted(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    tags = {f"k{i}": "v" for i in range(MAX_TAGS)}
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-count-ok@example.com",
        body_extra={"tags": tags},
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["tags"] == tags


async def test_create_over_long_tag_value_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-value-len@example.com",
        body_extra={"tags": {"k": "v" * 513}},  # over the 512-char value cap
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_create_tag_value_at_the_length_cap_is_accepted(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-value-len-ok@example.com",
        body_extra={"tags": {"k": "v" * 512}},
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["tags"] == {"k": "v" * 512}


async def test_create_tags_at_every_published_maximum_is_accepted(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The count cap, the key length and the per-value cap are all published in the JSON Schema, so a
    # payload sitting on all three at once must be accepted: the unpublished serialised-size ceiling
    # is set above them precisely so it can't 422 a request the schema calls valid.
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-published-maxima@example.com",
        body_extra={"tags": {f"{'k' * 62}{i:02d}": "v" * 512 for i in range(MAX_TAGS)}},
    )
    assert response.status_code == status.HTTP_201_CREATED
    # Round-tripped intact: "accepted" would also be true of a boundary that silently emptied them.
    stored = response.json()["tags"]
    assert len(stored) == MAX_TAGS
    assert all(value == "v" * 512 for value in stored.values())


async def test_create_tags_over_the_byte_cap_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # At the count cap and at the per-value cap, so only the serialised total can reject it — and in
    # UTF-8 that needs multi-byte text: 16 x 512 three-byte chars is ~24 KiB.
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-bytes@example.com",
        body_extra={"tags": {f"k{i:02d}": "中" * 512 for i in range(MAX_TAGS)}},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "KiB limit" in response.text


async def test_create_non_ascii_tags_measured_as_utf8_not_json_escapes(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # 7 x 512 two-byte chars is ~7.2 KiB of UTF-8 (under the cap) but ~21 KiB once every char is
    # escaped to `\uXXXX`, so measuring the escaped form would reject a map this size.
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-bytes-utf8@example.com",
        body_extra={"tags": {f"k{i:02d}": "\u0447" * 512 for i in range(7)}},
    )
    assert response.status_code == status.HTTP_201_CREATED


@pytest.mark.parametrize("key", [".", "..", "...."])
async def test_create_dots_only_tag_key_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role, key: str
) -> None:
    # Passes the charset/length pattern, so only the extra rule in `is_valid_tag_key` rejects it.
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email=f"tags-dots-{len(key)}@example.com",
        body_extra={"tags": {key: "v"}},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_create_tag_key_with_dots_among_other_chars_is_allowed(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-dotted@example.com",
        body_extra={"tags": {"app.env": "prod"}},
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["tags"] == {"app.env": "prod"}


async def test_create_non_string_tag_value_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-nonstr@example.com",
        body_extra={"tags": {"k": 123}},  # non-string value, no silent coercion
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_create_unicode_tag_value_allowed(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    _, _, response = await _create_convo(
        auth_db_client,
        db_session,
        red_teamer_role,
        email="tags-unicode@example.com",
        body_extra={"tags": {"lang": "日本語 café 🚩"}},  # unicode values are free text
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["tags"] == {"lang": "日本語 café 🚩"}


async def test_create_duplicate_tag_keys_last_wins(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Raw JSON with a duplicate key: JSON parsing keeps the last, so the stored dict is deduped.
    caller = await _caller(db_session, red_teamer_role, email="tags-dup@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    raw = (
        '{"evaluation_ai_model_id": "'
        + str(assignment.id)
        + '", "conversation_group_id": "'
        + str(group_row.id)
        + '", "tags": {"k": "1", "k": "2"}}'
    )
    response = await auth_db_client.post(
        f"/api/v1/scenarios/{group_row.scenario_id}/conversations",
        content=raw,
        headers={**_auth(token), "content-type": "application/json"},
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["tags"] == {"k": "2"}


async def _eval_with_allowed_key(db_session: AsyncSession, key: str) -> Evaluation:
    evaluation = await _evaluation_for(db_session)
    evaluation.tags_restricted = True  # restriction is an explicit toggle, not just a non-empty set
    db_session.add(evaluation)
    db_session.add(EvaluationTagKey(evaluation_id=evaluation.id, key=key))
    await db_session.flush()
    return evaluation


async def test_create_rejects_tag_key_outside_allowed_set(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The evaluation defines an allowed-key set → a conversation tag key outside it is a 400.
    caller = await _caller(db_session, red_teamer_role, email="tags-disallowed@example.com")
    token = _token(caller)
    evaluation = await _eval_with_allowed_key(db_session, "env")
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    response = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "tags": {"team": "red"},  # "team" is not in the allowed set {"env"}
        },
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    _assert_problem(response, status.HTTP_400_BAD_REQUEST)


async def test_create_rejects_when_only_some_keys_allowed(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Per-key check, not all-or-nothing: one allowed key ("env") plus one disallowed ("team") → 400.
    caller = await _caller(db_session, red_teamer_role, email="tags-partial@example.com")
    token = _token(caller)
    evaluation = await _eval_with_allowed_key(db_session, "env")
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    response = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "tags": {"env": "prod", "team": "red"},
        },
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_create_allows_tag_key_in_allowed_set(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="tags-allowed@example.com")
    token = _token(caller)
    evaluation = await _eval_with_allowed_key(db_session, "env")
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    response = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "tags": {"env": "prod"},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["tags"] == {"env": "prod"}


async def test_update_rejects_tag_key_outside_allowed_set(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="tags-upd-disallowed@example.com")
    token = _token(caller)
    evaluation = await _eval_with_allowed_key(db_session, "env")
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"tags": {"nope": "x"}},
        headers=_auth(token),
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_create_allows_any_key_when_restriction_off(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # An allowed-key set exists but tags_restricted is off → tags stay unrestricted.
    caller = await _caller(db_session, red_teamer_role, email="tags-off@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)  # tags_restricted defaults to False
    db_session.add(EvaluationTagKey(evaluation_id=evaluation.id, key="env"))
    await db_session.flush()
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    response = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "tags": {"team": "red"},  # not in {"env"}, but restriction is off → allowed
        },
    )
    assert response.status_code == status.HTTP_201_CREATED


async def test_create_rejects_tags_when_tagging_disabled(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # tags_enabled off outranks the restriction axis: an explicitly allowed key is still rejected.
    caller = await _caller(db_session, red_teamer_role, email="tags-disabled@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    evaluation.tags_enabled = False
    db_session.add(evaluation)
    db_session.add(EvaluationTagKey(evaluation_id=evaluation.id, key="env"))
    await db_session.flush()
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    response = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "tags": {"env": "prod"},
        },
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    _assert_problem(response, status.HTTP_400_BAD_REQUEST)


async def test_create_without_tags_succeeds_when_tagging_disabled(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Disabling tagging blocks tags only — the evaluation stays usable for untagged conversations.
    caller = await _caller(db_session, red_teamer_role, email="tags-disabled-untagged@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    evaluation.tags_enabled = False
    db_session.add(evaluation)
    await db_session.flush()
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    response = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["tags"] == {}


async def test_update_rejects_tags_when_tagging_disabled(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The PATCH path runs the same gate, so tagging can't be smuggled in after create.
    caller = await _caller(db_session, red_teamer_role, email="tags-disabled-patch@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    evaluation.tags_enabled = False
    db_session.add(evaluation)
    await db_session.flush()
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    response = await auth_db_client.patch(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}",
        json={"tags": {"env": "prod"}},
        headers=_auth(token),
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_create_forbids_all_tags_when_restricted_with_empty_set(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # tags_restricted on + no allowed keys → any tag is rejected ("forbid all").
    caller = await _caller(db_session, red_teamer_role, email="tags-forbid-all@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    evaluation.tags_restricted = True
    db_session.add(evaluation)
    await db_session.flush()
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    response = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
            "tags": {"env": "prod"},
        },
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def _restore_conversation(
    client: AsyncClient, token: str, evaluation_id: object, conversation_id: str
) -> Response:
    return await client.post(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}/restore", headers=_auth(token)
    )


async def _group_deleted_at(db_session: AsyncSession, group_id: UUID) -> object:
    """Raw fetch of a group's `deleted_at`, bypassing the live-row filter."""
    db_session.expire_all()
    row = (await db_session.execute(select(ConversationGroup).where(ConversationGroup.id == group_id))).scalar_one()
    return row.deleted_at


async def _backdate_conversation_tombstone(db_session: AsyncSession, conversation_id: str, *, days: int) -> None:
    db_session.expire_all()
    row = (await db_session.execute(select(Conversation).where(Conversation.id == UUID(conversation_id)))).scalar_one()
    row.deleted_at = datetime.now(UTC) - timedelta(days=days)
    db_session.add(row)
    await db_session.flush()


async def test_restore_brings_a_deleted_conversation_back(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="restore-conv@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(token)
    )

    response = await _restore_conversation(auth_db_client, token, evaluation.id, conversation_id)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None
    assert await _conversation_deleted_at(db_session, conversation_id) is None


async def test_restore_revives_the_group_the_delete_pruned(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Deleting the last live conversation prunes its group; restoring it has to
    # bring the group back or the conversation returns under a dead parent.
    caller = await _caller(db_session, red_teamer_role, email="prune@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    # Bind the ids up front: the raw `deleted_at` helpers below `expire_all()`, which
    # would make a later `evaluation.id` read lazy-load from sync code.
    evaluation_id = evaluation.id
    assignment = await _assignment(db_session, evaluation_id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation_id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    group_id = UUID(created.json()["conversation_group_id"])
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}", headers=_auth(token)
    )
    assert await _group_deleted_at(db_session, group_id) is not None

    response = await _restore_conversation(auth_db_client, token, evaluation_id, conversation_id)

    assert response.status_code == status.HTTP_200_OK
    assert await _group_deleted_at(db_session, group_id) is None


async def test_restore_revives_the_group_its_own_delete_removed(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The other revive path: deleting the group cascades to its members, and restoring
    # one of them has to bring the group back around it. Undo on the group-delete toast
    # is exactly this call, once per member.
    caller = await _caller(db_session, red_teamer_role, email="group-revive@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    # See the prune test: the raw `deleted_at` helpers expire the session's instances.
    evaluation_id = evaluation.id
    assignment = await _assignment(db_session, evaluation_id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation_id)
    body = {
        "evaluation_ai_model_id": str(assignment.id),
        "conversation_group_id": str(group_row.id),
    }
    first = (await _create(auth_db_client, token, group_row.scenario_id, body)).json()["id"]
    second = (await _create(auth_db_client, token, group_row.scenario_id, body)).json()["id"]
    group_id = group_row.id
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/conversation-groups/{group_id}", headers=_auth(token)
    )
    assert await _group_deleted_at(db_session, group_id) is not None

    response = await _restore_conversation(auth_db_client, token, evaluation_id, first)

    assert response.status_code == status.HTTP_200_OK
    assert await _group_deleted_at(db_session, group_id) is None
    assert await _conversation_deleted_at(db_session, first) is None
    # Only the member that was restored comes back — the group's other member stays
    # tombstoned until its own restore, which is why Undo replays every captured id.
    assert await _conversation_deleted_at(db_session, second) is not None


async def test_restore_revives_a_group_someone_else_deleted_when_the_member_predates_it(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role, manager_role: Role
) -> None:
    # The limit of the transitive gate on `revive_pruned_group`. The group cascade uses
    # `live_update`, so it stamps only the members that were still live — a conversation
    # tombstoned *before* the group delete keeps its own deleter. Its owner can then
    # restore it and bring back a group a manager deleted, holding only
    # `conversations:delete`. Correct as behaviour (the row would otherwise be
    # parentless), so this pins it rather than forbidding it.
    owner = await _caller(db_session, red_teamer_role, email="mixed-deleter@example.com")
    owner_token = _token(owner)
    evaluation = await _evaluation_for(db_session)
    evaluation_id = evaluation.id
    assignment = await _assignment(db_session, evaluation_id)
    group_row = await _group(db_session, owner_id=owner.id, evaluation_id=evaluation_id)
    body = {
        "evaluation_ai_model_id": str(assignment.id),
        "conversation_group_id": str(group_row.id),
    }
    early = (await _create(auth_db_client, owner_token, group_row.scenario_id, body)).json()["id"]
    late = (await _create(auth_db_client, owner_token, group_row.scenario_id, body)).json()["id"]
    group_id = group_row.id

    # The owner deletes one member; the group survives because the other is still live.
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{early}", headers=_auth(owner_token)
    )
    assert await _group_deleted_at(db_session, group_id) is None
    manager = await _caller(db_session, manager_role, email="group-remover@example.com")
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/conversation-groups/{group_id}", headers=_auth(_token(manager))
    )
    assert await _group_deleted_at(db_session, group_id) is not None

    response = await _restore_conversation(auth_db_client, owner_token, evaluation_id, early)

    assert response.status_code == status.HTTP_200_OK
    assert await _group_deleted_at(db_session, group_id) is None
    # The member the manager's cascade tombstoned stays down — only they can restore it.
    assert await _conversation_deleted_at(db_session, late) is not None


async def test_read_any_does_not_widen_the_deleted_listing(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # `conversations:read_any` lifts the owner predicate on *live* reads, so a group owner
    # sees a member's transcript. It must not carry into the tombstones: the deleted view is
    # the restore surface, and only the deleter (or the break-glass) may restore. Enforced by
    # the deleter predicate rather than by withholding `read_any` — a non-manager can only
    # delete what they own, so `deleted_by_id == caller_id` already implies ownership — which
    # is why this pins the *behaviour*, leaving the mechanism free to change.
    author = await _caller(db_session, red_teamer_role, email="ra-author@example.com")
    author_token = _token(author)
    reader_role = await make_role(db_session, [*_CONVERSATION_PERMS, Permission.CONVERSATIONS_READ_ANY.value])
    reader = await _caller(db_session, reader_role, email="ra-reader@example.com")
    reader_token = _token(reader)
    evaluation = await _evaluation_for(db_session, owner_id=author.id)
    evaluation_id = evaluation.id
    await grant_roles(db_session, ObjectType.EVALUATION_GROUP, evaluation.evaluation_group_id, reader.id, [reader_role])
    assignment = await _assignment(db_session, evaluation_id)
    group_row = await _group(db_session, owner_id=author.id, evaluation_id=evaluation_id)
    created = await _create(
        auth_db_client,
        author_token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    # The widening is real on the live read...
    live = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}", headers=_auth(reader_token)
    )
    assert live.status_code == status.HTTP_200_OK
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}", headers=_auth(author_token)
    )

    listed = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation_id}/conversations?deleted=true", headers=_auth(reader_token)
    )
    restored = await _restore_conversation(auth_db_client, reader_token, evaluation_id, conversation_id)

    # ...and absent from the tombstones, which only the deleter (or the break-glass) may restore.
    assert listed.status_code == status.HTTP_200_OK
    assert [item["id"] for item in listed.json()["items"]] == []
    _assert_problem(restored, status.HTTP_404_NOT_FOUND)
    assert await _conversation_deleted_at(db_session, conversation_id) is not None


async def test_restore_is_404_behind_a_soft_deleted_evaluation(
    auth_db_client: AsyncClient, db_session: AsyncSession, cascade_admin_role: Role
) -> None:
    # Parent liveness is enforced on the restore path too. The tombstone is the
    # caller's own and well inside the window, but its evaluation is gone — bringing
    # the conversation back would hang it off a dead parent, invisible to every read.
    # (The evaluation delete leaves the assignment live, so this pins the parent
    # predicate, not the cascade one.)
    caller = await _caller(db_session, cascade_admin_role, email="dead-parent@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    evaluation_id = evaluation.id
    assignment = await _assignment(db_session, evaluation_id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation_id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}", headers=_auth(token)
    )
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}", headers=_auth(token))

    response = await _restore_conversation(auth_db_client, token, evaluation_id, conversation_id)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)
    assert await _conversation_deleted_at(db_session, conversation_id) is not None


async def test_cascade_deleted_conversation_is_not_restorable(
    auth_db_client: AsyncClient, db_session: AsyncSession, cascade_admin_role: Role
) -> None:
    # Unassigning the model tombstones the conversation and records the unassigning
    # admin as its deleter, which would otherwise make it look like that admin's own
    # delete. Restoring it would hand back a conversation whose chosen model is gone:
    # `resolve_dispatch_target` still resolves the tombstoned assignment, so the next
    # message would run inference against the model that was deliberately removed.
    caller = await _caller(db_session, cascade_admin_role, email="cascade-restore@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    evaluation_id = evaluation.id
    assignment = await _assignment(db_session, evaluation_id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation_id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    unassigned = await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/models/{assignment.id}", headers=_auth(token)
    )
    assert unassigned.status_code == status.HTTP_204_NO_CONTENT

    response = await _restore_conversation(auth_db_client, token, evaluation_id, conversation_id)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)
    assert await _conversation_deleted_at(db_session, conversation_id) is not None


async def test_restoring_the_assignment_makes_its_conversations_restorable_again(
    auth_db_client: AsyncClient, db_session: AsyncSession, cascade_admin_role: Role
) -> None:
    # The conversation's restorability is *derived* from its assignment being live, not
    # a flag of its own — so re-instating the assignment re-opens it. Intended: the
    # model can run again, so its conversations can too. The conversation still needs
    # its own restore; the assignment coming back only lifts the block.
    caller = await _caller(db_session, cascade_admin_role, email="reopened@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    evaluation_id = evaluation.id
    assignment = await _assignment(db_session, evaluation_id)
    assignment_id = assignment.id
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation_id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment_id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}/models/{assignment_id}", headers=_auth(token))
    blocked = await _restore_conversation(auth_db_client, token, evaluation_id, conversation_id)
    _assert_problem(blocked, status.HTTP_404_NOT_FOUND)

    revived = await auth_db_client.post(
        f"/api/v1/evaluations/{evaluation_id}/models/{assignment_id}/restore", headers=_auth(token)
    )
    assert revived.status_code == status.HTTP_200_OK

    response = await _restore_conversation(auth_db_client, token, evaluation_id, conversation_id)

    assert response.status_code == status.HTTP_200_OK
    assert await _conversation_deleted_at(db_session, conversation_id) is None


async def test_cascade_deleted_conversation_is_absent_from_the_deleted_listing(
    auth_db_client: AsyncClient, db_session: AsyncSession, cascade_admin_role: Role
) -> None:
    # The deleted listing is the restore surface, so it must not offer a row the
    # restore endpoint would refuse — a Restore button that 404s is worse than none.
    caller = await _caller(db_session, cascade_admin_role, email="cascade-list@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    evaluation_id = evaluation.id
    assignment = await _assignment(db_session, evaluation_id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation_id)
    body = {
        "evaluation_ai_model_id": str(assignment.id),
        "conversation_group_id": str(group_row.id),
    }
    orphaned = (await _create(auth_db_client, token, group_row.scenario_id, body)).json()["id"]
    own = (await _create(auth_db_client, token, group_row.scenario_id, body)).json()["id"]
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}/conversations/{own}", headers=_auth(token))
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}/models/{assignment.id}", headers=_auth(token))

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation_id}/conversations?deleted=true", headers=_auth(token)
    )

    assert response.status_code == status.HTTP_200_OK
    listed = [item["id"] for item in response.json()["items"]]
    assert orphaned not in listed
    # The user's own delete of a conversation on the same assignment is gone too: the
    # assignment it points at is tombstoned, so it can no longer run either.
    assert own not in listed


async def test_restore_is_404_outside_the_window(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="stale-conv@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    # See the prune test: the backdate helper expires the session's instances.
    evaluation_id = evaluation.id
    assignment = await _assignment(db_session, evaluation_id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation_id)
    created = await _create(
        auth_db_client,
        token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation_id}/conversations/{conversation_id}", headers=_auth(token)
    )
    await _backdate_conversation_tombstone(db_session, conversation_id, days=30)

    response = await _restore_conversation(auth_db_client, token, evaluation_id, conversation_id)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)
    assert await _conversation_deleted_at(db_session, conversation_id) is not None


async def test_manager_restores_another_users_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role, manager_role: Role
) -> None:
    # The admin rule, and the positive half of the deleter scope: the break-glass
    # reaches a tombstone the manager did not create.
    owner = await _caller(db_session, red_teamer_role, email="restored-for@example.com")
    owner_token = _token(owner)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=owner.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        owner_token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(owner_token)
    )
    manager = await _caller(db_session, manager_role, email="conv-fixer@example.com")

    response = await _restore_conversation(auth_db_client, _token(manager), evaluation.id, conversation_id)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None


async def test_owner_cannot_restore_a_managers_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role, manager_role: Role
) -> None:
    owner = await _caller(db_session, red_teamer_role, email="owned@example.com")
    owner_token = _token(owner)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=owner.id, evaluation_id=evaluation.id)
    created = await _create(
        auth_db_client,
        owner_token,
        group_row.scenario_id,
        {
            "evaluation_ai_model_id": str(assignment.id),
            "conversation_group_id": str(group_row.id),
        },
    )
    conversation_id = created.json()["id"]
    manager = await _caller(db_session, manager_role, email="conv-moderator@example.com")
    await auth_db_client.delete(
        f"/api/v1/evaluations/{evaluation.id}/conversations/{conversation_id}", headers=_auth(_token(manager))
    )

    response = await _restore_conversation(auth_db_client, owner_token, evaluation.id, conversation_id)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_deleted_listing_returns_the_callers_tombstoned_conversations(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    caller = await _caller(db_session, red_teamer_role, email="deleted-list@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    assignment = await _assignment(db_session, evaluation.id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation.id)
    body = {
        "evaluation_ai_model_id": str(assignment.id),
        "conversation_group_id": str(group_row.id),
    }
    kept = await _create(auth_db_client, token, group_row.scenario_id, body)
    dropped = await _create(auth_db_client, token, group_row.scenario_id, body)
    dropped_id = dropped.json()["id"]
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation.id}/conversations/{dropped_id}", headers=_auth(token))

    response = await auth_db_client.get(
        f"/api/v1/evaluations/{evaluation.id}/conversations?deleted=true", headers=_auth(token)
    )

    assert response.status_code == status.HTTP_200_OK
    items = response.json()["items"]
    assert [item["id"] for item in items] == [dropped_id]
    assert items[0]["deleted_by_id"] == str(caller.id)
    assert kept.json()["id"] not in [item["id"] for item in items]


async def test_flat_deleted_listing_returns_tombstones_not_live_rows(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # The flat list publishes `deleted` through the shared filters dependency, so it
    # has to derive the cutoff too — otherwise `deleted=true` silently served the live
    # set and every row came back with a Restore button that 404s.
    caller = await _caller(db_session, red_teamer_role, email="flat-deleted@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    evaluation_id = evaluation.id
    assignment = await _assignment(db_session, evaluation_id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation_id)
    body = {
        "evaluation_ai_model_id": str(assignment.id),
        "conversation_group_id": str(group_row.id),
    }
    kept = await _create(auth_db_client, token, group_row.scenario_id, body)
    dropped = await _create(auth_db_client, token, group_row.scenario_id, body)
    dropped_id = dropped.json()["id"]
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}/conversations/{dropped_id}", headers=_auth(token))

    response = await auth_db_client.get("/api/v1/conversations?deleted=true", headers=_auth(token))

    assert response.status_code == status.HTTP_200_OK
    items = response.json()["items"]
    assert [item["id"] for item in items] == [dropped_id]
    assert items[0]["deleted_at"] is not None
    assert kept.json()["id"] not in [item["id"] for item in items]


async def test_restore_conflicts_when_the_group_refilled_the_freed_slot(
    auth_db_client: AsyncClient, db_session: AsyncSession, red_teamer_role: Role
) -> None:
    # Restore is a third path that makes a conversation live, so it must honour the
    # structural group cap: delete one, fill the slot, and the restore has to 409
    # rather than push the group over the maximum.
    caller = await _caller(db_session, red_teamer_role, email="refilled@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    evaluation_id = evaluation.id
    assignment = await _assignment(db_session, evaluation_id)
    group_row = await _group(db_session, owner_id=caller.id, evaluation_id=evaluation_id)
    body = {
        "evaluation_ai_model_id": str(assignment.id),
        "conversation_group_id": str(group_row.id),
    }
    max_size = get_settings().max_conversation_group_size
    created = [
        (await _create(auth_db_client, token, group_row.scenario_id, body)).json()["id"] for _ in range(max_size)
    ]
    doomed = created[0]
    await auth_db_client.delete(f"/api/v1/evaluations/{evaluation_id}/conversations/{doomed}", headers=_auth(token))
    refill = await _create(auth_db_client, token, group_row.scenario_id, body)
    assert refill.status_code == status.HTTP_201_CREATED

    response = await _restore_conversation(auth_db_client, token, evaluation_id, doomed)

    _assert_problem(response, status.HTTP_409_CONFLICT)
    assert await _conversation_deleted_at(db_session, doomed) is not None
