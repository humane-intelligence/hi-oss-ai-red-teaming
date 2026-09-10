"""Integration tests for the message-flag router — flat `/api/v1/message-flags`."""

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
from app.core.annotations.models import MessageFlag
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import TAG_CONTEXT_EXTRA_KEY
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from tests.api.v1.conftest import make_token as _token
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


_FLAG_PERMS = [
    Permission.FLAGS_READ.value,
    Permission.FLAGS_CREATE.value,
    Permission.FLAGS_UPDATE.value,
    Permission.FLAGS_DELETE.value,
]


@pytest_asyncio.fixture
async def flagger_role(db_session: AsyncSession) -> Role:
    """Full message-flag CRUD — the red-teamer's owner-scoped surface (no break-glass)."""
    role = Role(name="flagger", description="flags CRUD", permissions=list(_FLAG_PERMS))
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def manager_role(db_session: AsyncSession) -> Role:
    """Flag CRUD plus the `evaluation_groups:manage` break-glass that lifts the owner scope."""
    role = Role(
        name="flag-manager",
        description="flags CRUD + manage",
        permissions=[*_FLAG_PERMS, Permission.EVALUATION_GROUPS_MANAGE.value],
    )
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def reader_role(db_session: AsyncSession) -> Role:
    """Read-only flags permission — can list/get but not create."""
    role = Role(name="flag-reader", description="read-only", permissions=[Permission.FLAGS_READ.value])
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


async def _task(db_session: AsyncSession, scenario_id: UUID) -> Task:
    row = Task(name="T", description="d", scenario_id=scenario_id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    return row


async def _conversation(
    db_session: AsyncSession, *, user_id: UUID, evaluation: Evaluation, scenario_id: UUID | None = None
) -> Conversation:
    assignment = await _assignment(db_session, evaluation.id)
    if scenario_id is None:
        scenario_id = (await _scenario(db_session, evaluation.id)).id
    conversation_group = ConversationGroup(
        user_id=user_id, evaluation_id=evaluation.id, name="group", scenario_id=scenario_id
    )
    db_session.add(conversation_group)
    await db_session.flush()
    conversation = Conversation(
        user_id=user_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        scenario_id=scenario_id,
        conversation_group_id=conversation_group.id,
    )
    db_session.add(conversation)
    await db_session.flush()
    await db_session.refresh(conversation)
    return conversation


async def _messages(db_session: AsyncSession, conversation_id: UUID, count: int = 1) -> list[Message]:
    """Seed ``count`` assistant messages, one per turn, on a conversation (no write-path exists yet)."""
    messages: list[Message] = []
    for index in range(count):
        turn = Turn(conversation_id=conversation_id, turn_index=index)
        db_session.add(turn)
        await db_session.flush()
        message = Message(
            turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content=f"reply {index}"
        )
        db_session.add(message)
        await db_session.flush()
        await db_session.refresh(message)
        messages.append(message)
    return messages


async def _caller(db_session: AsyncSession, role: Role, *, email: str) -> User:
    return await create_user_service(db_session, email=email, roles=[role])


def _assert_problem(response: Response, expected_status: int) -> None:
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == expected_status
    assert body["title"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create(client: AsyncClient, token: str, body: dict) -> Response:
    return await client.post("/api/v1/message-flags", json=body, headers=_auth(token))


async def _flag_deleted_at(db_session: AsyncSession, flag_id: str) -> object:
    """Raw fetch of a flag's `deleted_at`, bypassing the live-row filter."""
    db_session.expire_all()
    row = (await db_session.execute(select(MessageFlag).where(MessageFlag.id == UUID(flag_id)))).scalar_one()
    return row.deleted_at


async def test_create_returns_201_and_denormalises_ancestry(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="rt@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)

    response = await _create(
        auth_db_client,
        _token(caller),
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "step-by-step"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["conversation_id"] == str(conversation.id)
    assert [m["id"] for m in body["messages"]] == [str(message.id)]
    assert body["messages"][0]["role"] == "assistant"
    assert body["messages"][0]["content"] == "reply 0"
    assert body["reason"] == "step-by-step"
    assert body["red_flagged"] is True
    assert body["status"] == "pending"
    assert body["created_by_id"] == str(caller.id)
    assert body["evaluation_id"] == str(evaluation.id)
    assert body["evaluation_group_id"] == str(evaluation.evaluation_group_id)
    assert body["scenario_id"] == str(conversation.scenario_id)
    assert body["task_id"] is None
    assert response.headers["Location"] == f"/api/v1/message-flags/{body['id']}"


async def test_create_flags_a_subset_range(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="range@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    messages = await _messages(db_session, conversation.id, 5)
    selected = [messages[1].id, messages[2].id]  # a 2-message slice out of 5

    response = await _create(
        auth_db_client,
        _token(caller),
        {"conversation_id": str(conversation.id), "message_ids": [str(m) for m in selected], "reason": "r"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert {m["id"] for m in response.json()["messages"]} == {str(m) for m in selected}


async def test_flagged_messages_put_a_turns_prompt_before_its_reply(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    """A turn's messages share `created_at`, so the embedded order comes from the role key.

    `_messages` seeds assistant-only turns, which can never exercise this: every row there
    ties on `created_at` *and* role, leaving the `id` tiebreak to decide. The ids are pinned
    so the assertion fails against an ordering that falls through to them.
    """
    caller = await _caller(db_session, flagger_role, email="turn-order@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    turn = Turn(conversation_id=conversation.id, turn_index=0)
    db_session.add(turn)
    await db_session.flush()
    prompt = Message(
        id=UUID(int=2), turn_id=turn.id, role=MessageRole.USER, status=MessageStatus.COMPLETE, content="ask"
    )
    reply = Message(
        id=UUID(int=1), turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content="answer"
    )
    db_session.add_all([prompt, reply])
    await db_session.flush()
    await db_session.refresh(prompt)
    await db_session.refresh(reply)
    assert prompt.created_at == reply.created_at, "precondition: the tie this ordering has to survive"

    response = await _create(
        auth_db_client,
        _token(caller),
        {
            "conversation_id": str(conversation.id),
            "message_ids": [str(reply.id), str(prompt.id)],
            "reason": "r",
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert [m["id"] for m in response.json()["messages"]] == [str(prompt.id), str(reply.id)]


async def test_create_flags_whole_conversation(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="whole@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    messages = await _messages(db_session, conversation.id, 4)

    response = await _create(
        auth_db_client,
        _token(caller),
        {"conversation_id": str(conversation.id), "message_ids": [str(m.id) for m in messages], "reason": "r"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert {m["id"] for m in response.json()["messages"]} == {str(m.id) for m in messages}


async def test_create_with_task_pins_scenario_and_task(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="task@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id)
    task = await _task(db_session, scenario.id)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    (message,) = await _messages(db_session, conversation.id, 1)

    response = await _create(
        auth_db_client,
        _token(caller),
        {
            "conversation_id": str(conversation.id),
            "message_ids": [str(message.id)],
            "reason": "r",
            "task_id": str(task.id),
        },
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["scenario_id"] == str(scenario.id)
    assert body["task_id"] == str(task.id)


async def test_create_unknown_conversation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="ghost@example.com")

    response = await _create(
        auth_db_client, _token(caller), {"conversation_id": str(uuid4()), "message_ids": [str(uuid4())], "reason": "r"}
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_create_on_other_users_conversation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    owner = await _caller(db_session, flagger_role, email="mine@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=owner.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    intruder = await _caller(db_session, flagger_role, email="nosy@example.com")

    response = await _create(
        auth_db_client,
        _token(intruder),
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "r"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_create_message_from_other_conversation_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="xconv@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    other_conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    (foreign_message,) = await _messages(db_session, other_conversation.id, 1)

    response = await _create(
        auth_db_client,
        _token(caller),
        {"conversation_id": str(conversation.id), "message_ids": [str(foreign_message.id)], "reason": "r"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_create_empty_message_ids_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="empty@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)

    response = await _create(
        auth_db_client, _token(caller), {"conversation_id": str(conversation.id), "message_ids": [], "reason": "r"}
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("message_ids" in error["loc"] for error in response.json()["errors"])


async def test_create_duplicate_message_ids_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="dup@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)

    response = await _create(
        auth_db_client,
        _token(caller),
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id), str(message.id)], "reason": "r"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("message_ids" in error["loc"] for error in response.json()["errors"])


async def test_create_with_task_from_other_scenario_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="xtask@example.com")
    evaluation = await _evaluation_for(db_session)
    scenario = await _scenario(db_session, evaluation.id)
    other_scenario = await _scenario(db_session, evaluation.id)
    foreign_task = await _task(db_session, other_scenario.id)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation, scenario_id=scenario.id)
    (message,) = await _messages(db_session, conversation.id, 1)

    response = await _create(
        auth_db_client,
        _token(caller),
        {
            "conversation_id": str(conversation.id),
            "message_ids": [str(message.id)],
            "reason": "r",
            "task_id": str(foreign_task.id),
        },
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_get_other_users_flag_returns_404(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    owner = await _caller(db_session, flagger_role, email="fowner@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=owner.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    created = await _create(
        auth_db_client,
        _token(owner),
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "r"},
    )
    flag_id = created.json()["id"]
    intruder = await _caller(db_session, flagger_role, email="fnosy@example.com")

    response = await auth_db_client.get(f"/api/v1/message-flags/{flag_id}", headers=_auth(_token(intruder)))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_list_returns_only_own_and_filters_by_message(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    owner = await _caller(db_session, flagger_role, email="la@example.com")
    other = await _caller(db_session, flagger_role, email="lb@example.com")
    evaluation = await _evaluation_for(db_session)
    own_conv = await _conversation(db_session, user_id=owner.id, evaluation=evaluation)
    own_messages = await _messages(db_session, own_conv.id, 2)
    other_conv = await _conversation(db_session, user_id=other.id, evaluation=evaluation)
    (other_msg,) = await _messages(db_session, other_conv.id, 1)
    await _create(
        auth_db_client,
        _token(owner),
        {"conversation_id": str(own_conv.id), "message_ids": [str(m.id) for m in own_messages], "reason": "r"},
    )
    await _create(
        auth_db_client,
        _token(other),
        {"conversation_id": str(other_conv.id), "message_ids": [str(other_msg.id)], "reason": "r"},
    )

    listed = await auth_db_client.get("/api/v1/message-flags", headers=_auth(_token(owner)))
    by_message = await auth_db_client.get(
        f"/api/v1/message-flags?message_id={own_messages[0].id}", headers=_auth(_token(owner))
    )
    by_other_message = await auth_db_client.get(
        f"/api/v1/message-flags?message_id={other_msg.id}", headers=_auth(_token(owner))
    )

    assert listed.json()["total"] == 1
    item = listed.json()["items"][0]
    assert item["created_by_id"] == str(owner.id)
    # The list path embeds each message's base data (not bare ids), eager-loaded.
    assert {m["id"] for m in item["messages"]} == {str(m.id) for m in own_messages}
    embedded = {m["id"]: m for m in item["messages"]}
    first = embedded[str(own_messages[0].id)]
    assert first["role"] == "assistant"
    assert first["status"] == "complete"
    assert first["content"] == "reply 0"
    assert by_message.json()["total"] == 1  # the flag whose selection includes that message
    assert by_other_message.json()["total"] == 0  # owner can't see the other user's flag


async def test_list_filters_by_search(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    # `search` is a case-insensitive substring match across `reason` and `comment`.
    caller = await _caller(db_session, flagger_role, email="search@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    msgs = await _messages(db_session, conversation.id, 3)
    body = {"conversation_id": str(conversation.id)}
    await _create(auth_db_client, token, {**body, "message_ids": [str(msgs[0].id)], "reason": "jailbreak via roleplay"})
    await _create(
        auth_db_client,
        token,
        {**body, "message_ids": [str(msgs[1].id)], "reason": "leaked system prompt", "comment": "ROLEPLAY note"},
    )
    await _create(auth_db_client, token, {**body, "message_ids": [str(msgs[2].id)], "reason": "benign refusal"})

    matched = await auth_db_client.get("/api/v1/message-flags?search=roleplay", headers=_auth(token))
    missed = await auth_db_client.get("/api/v1/message-flags?search=nonexistent", headers=_auth(token))

    # Case-insensitive, matches `reason` (first flag) and `comment` (second flag).
    assert matched.json()["total"] == 2
    assert {flag["reason"] for flag in matched.json()["items"]} == {"jailbreak via roleplay", "leaked system prompt"}
    assert missed.json()["total"] == 0


async def test_list_search_escapes_like_metacharacters(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    # A `%` in `search` matches literally, not as a wildcard.
    caller = await _caller(db_session, flagger_role, email="escape@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    msgs = await _messages(db_session, conversation.id, 2)
    cbody = {"conversation_id": str(conversation.id)}
    await _create(auth_db_client, token, {**cbody, "message_ids": [str(msgs[0].id)], "reason": "100% success rate"})
    await _create(auth_db_client, token, {**cbody, "message_ids": [str(msgs[1].id)], "reason": "plain text"})

    literal = await auth_db_client.get("/api/v1/message-flags?search=100%25", headers=_auth(token))

    assert literal.json()["total"] == 1
    assert literal.json()["items"][0]["reason"] == "100% success rate"


async def test_list_rejects_oversized_limit(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="page@example.com")

    response = await auth_db_client.get("/api/v1/message-flags?limit=101", headers=_auth(_token(caller)))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("limit" in error["loc"] for error in response.json()["errors"])


async def test_update_edits_content_and_clears_note(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="upd@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    created = await _create(
        auth_db_client,
        token,
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "r", "comment": "note"},
    )
    flag_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/message-flags/{flag_id}",
        json={"reason": "revised", "red_flagged": False, "comment": None},
        headers=_auth(token),
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["reason"] == "revised"
    assert body["red_flagged"] is False
    assert body["comment"] is None
    assert [m["id"] for m in body["messages"]] == [str(message.id)]  # selection untouched by a content edit


async def test_update_explicit_null_reason_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="null@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    created = await _create(
        auth_db_client,
        token,
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "r"},
    )
    flag_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/message-flags/{flag_id}", json={"reason": None}, headers=_auth(token)
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("reason" in error["loc"] for error in response.json()["errors"])


async def test_update_partial_leaves_omitted_fields_unchanged(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    # Only `reason` is sent; `red_flagged` and `comment` must survive untouched
    # (guards the `model_fields_set` path against overwriting omitted fields).
    caller = await _caller(db_session, flagger_role, email="partial@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    created = await _create(
        auth_db_client,
        token,
        {
            "conversation_id": str(conversation.id),
            "message_ids": [str(message.id)],
            "reason": "r",
            "red_flagged": False,
            "comment": "note",
        },
    )
    flag_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/message-flags/{flag_id}", json={"reason": "revised"}, headers=_auth(token)
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["reason"] == "revised"
    assert body["red_flagged"] is False  # omitted → unchanged
    assert body["comment"] == "note"  # omitted → unchanged


async def test_update_explicit_null_red_flagged_returns_422(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="nullflag@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    created = await _create(
        auth_db_client,
        token,
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "r"},
    )
    flag_id = created.json()["id"]

    response = await auth_db_client.patch(
        f"/api/v1/message-flags/{flag_id}", json={"red_flagged": None}, headers=_auth(token)
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    _assert_problem(response, status.HTTP_422_UNPROCESSABLE_CONTENT)
    assert any("red_flagged" in error["loc"] for error in response.json()["errors"])


async def test_delete_soft_deletes(auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role) -> None:
    caller = await _caller(db_session, flagger_role, email="del@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    created = await _create(
        auth_db_client,
        token,
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "r"},
    )
    flag_id = created.json()["id"]

    deleted = await auth_db_client.delete(f"/api/v1/message-flags/{flag_id}", headers=_auth(token))
    follow_up = await auth_db_client.get(f"/api/v1/message-flags/{flag_id}", headers=_auth(token))

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND
    assert await _flag_deleted_at(db_session, flag_id) is not None


async def test_manager_reads_any_users_flag(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role, manager_role: Role
) -> None:
    owner = await _caller(db_session, flagger_role, email="fm-owner@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=owner.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    created = await _create(
        auth_db_client,
        _token(owner),
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "r"},
    )
    flag_id = created.json()["id"]
    manager = await _caller(db_session, manager_role, email="fm-admin@example.com")

    response = await auth_db_client.get(f"/api/v1/message-flags/{flag_id}", headers=_auth(_token(manager)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["created_by_id"] == str(owner.id)


async def test_soft_deleting_conversation_hides_flag(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    # Parent-liveness via the join: soft-deleting the conversation hides its flags
    # for everyone, with no dedicated cascade hook on the flag itself.
    caller = await _caller(db_session, flagger_role, email="parent@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    created = await _create(
        auth_db_client,
        token,
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "r"},
    )
    flag_id = created.json()["id"]

    conversation.soft_delete(None)
    db_session.add(conversation)
    await db_session.flush()

    response = await auth_db_client.get(f"/api/v1/message-flags/{flag_id}", headers=_auth(token))

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert await _flag_deleted_at(db_session, flag_id) is None  # hidden, not tombstoned


async def test_create_flags_a_superseded_message(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    # A regenerated-past message stays live, so it is flaggable for provenance
    # even though the conversation read-path surfaces only the survivor.
    caller = await _caller(db_session, flagger_role, email="superseded@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    superseded, survivor = await _messages(db_session, conversation.id, 2)
    survivor.replaces_message_id = superseded.id
    db_session.add(survivor)
    await db_session.flush()

    response = await _create(
        auth_db_client,
        token,
        {"conversation_id": str(conversation.id), "message_ids": [str(superseded.id)], "reason": "r"},
    )

    assert response.status_code == status.HTTP_201_CREATED
    assert [m["id"] for m in response.json()["messages"]] == [str(superseded.id)]


async def test_manager_cannot_author_flag_on_another_users_conversation(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role, manager_role: Role
) -> None:
    # Authoring is owner-only: the `evaluation_groups:manage` break-glass lifts the
    # owner scope on read/update/delete but NOT on create — a manager can review
    # others' flags, not stamp one in their name (the owner would never see it).
    owner = await _caller(db_session, flagger_role, email="author-owner@example.com")
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=owner.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    manager = await _caller(db_session, manager_role, email="author-manager@example.com")

    response = await _create(
        auth_db_client,
        _token(manager),
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "r"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def _delete(client: AsyncClient, token: str, flag_id: str) -> Response:
    return await client.delete(f"/api/v1/message-flags/{flag_id}", headers=_auth(token))


async def _restore(client: AsyncClient, token: str, flag_id: str) -> Response:
    return await client.post(f"/api/v1/message-flags/{flag_id}/restore", headers=_auth(token))


async def _flagged(client: AsyncClient, db_session: AsyncSession, token: str, *, email: str) -> str:
    """Seed a conversation + flag owned by the token's caller, returning the flag id."""
    caller = (await db_session.execute(select(User).where(User.email == email))).scalar_one()
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    created = await _create(
        client,
        token,
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "r"},
    )
    return created.json()["id"]


async def _age_tombstone(db_session: AsyncSession, flag_id: str, *, days: int) -> None:
    """Backdate a tombstone past the restore window."""
    db_session.expire_all()
    row = (await db_session.execute(select(MessageFlag).where(MessageFlag.id == UUID(flag_id)))).scalar_one()
    row.deleted_at = datetime.now(UTC) - timedelta(days=days)
    db_session.add(row)
    await db_session.flush()


async def test_restore_brings_a_deleted_flag_back(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="restore@example.com")
    token = _token(caller)
    flag_id = await _flagged(auth_db_client, db_session, token, email="restore@example.com")
    await _delete(auth_db_client, token, flag_id)

    response = await _restore(auth_db_client, token, flag_id)

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None
    assert await _flag_deleted_at(db_session, flag_id) is None
    # Back in the live listing, and gone from the deleted one.
    live = await auth_db_client.get("/api/v1/message-flags", headers=_auth(token))
    assert [item["id"] for item in live.json()["items"]] == [flag_id]


async def test_restore_is_404_for_a_live_flag(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="live-restore@example.com")
    token = _token(caller)
    flag_id = await _flagged(auth_db_client, db_session, token, email="live-restore@example.com")

    response = await _restore(auth_db_client, token, flag_id)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_restore_is_404_outside_the_window(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="stale@example.com")
    token = _token(caller)
    flag_id = await _flagged(auth_db_client, db_session, token, email="stale@example.com")
    await _delete(auth_db_client, token, flag_id)
    await _age_tombstone(db_session, flag_id, days=30)

    response = await _restore(auth_db_client, token, flag_id)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)
    assert await _flag_deleted_at(db_session, flag_id) is not None


async def test_owner_cannot_restore_a_managers_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role, manager_role: Role
) -> None:
    # The rule: a user restores only their own deletes, so an admin's
    # moderation action can't be undone by the flag's author.
    owner = await _caller(db_session, flagger_role, email="moderated@example.com")
    owner_token = _token(owner)
    flag_id = await _flagged(auth_db_client, db_session, owner_token, email="moderated@example.com")
    manager = await _caller(db_session, manager_role, email="moderator@example.com")
    await _delete(auth_db_client, _token(manager), flag_id)

    response = await _restore(auth_db_client, owner_token, flag_id)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)
    assert await _flag_deleted_at(db_session, flag_id) is not None


async def test_manager_restores_another_users_delete(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role, manager_role: Role
) -> None:
    owner = await _caller(db_session, flagger_role, email="broken@example.com")
    owner_token = _token(owner)
    flag_id = await _flagged(auth_db_client, db_session, owner_token, email="broken@example.com")
    await _delete(auth_db_client, owner_token, flag_id)
    manager = await _caller(db_session, manager_role, email="fixer@example.com")

    response = await _restore(auth_db_client, _token(manager), flag_id)

    assert response.status_code == status.HTTP_200_OK
    assert await _flag_deleted_at(db_session, flag_id) is None


async def test_deleted_listing_shows_only_the_callers_own_tombstones(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role, manager_role: Role
) -> None:
    mine = await _caller(db_session, flagger_role, email="mine@example.com")
    mine_token = _token(mine)
    mine_flag = await _flagged(auth_db_client, db_session, mine_token, email="mine@example.com")
    await _delete(auth_db_client, mine_token, mine_flag)

    theirs = await _caller(db_session, flagger_role, email="theirs@example.com")
    theirs_token = _token(theirs)
    theirs_flag = await _flagged(auth_db_client, db_session, theirs_token, email="theirs@example.com")
    await _delete(auth_db_client, theirs_token, theirs_flag)

    response = await auth_db_client.get("/api/v1/message-flags?deleted=true", headers=_auth(mine_token))

    assert response.status_code == status.HTTP_200_OK
    items = response.json()["items"]
    assert [item["id"] for item in items] == [mine_flag]
    assert items[0]["deleted_at"] is not None
    assert items[0]["deleted_by_id"] == str(mine.id)


async def test_deleted_listing_excludes_live_and_stale_rows(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="mixed@example.com")
    token = _token(caller)
    live_flag = await _flagged(auth_db_client, db_session, token, email="mixed@example.com")
    fresh_flag = await _flagged(auth_db_client, db_session, token, email="mixed@example.com")
    stale_flag = await _flagged(auth_db_client, db_session, token, email="mixed@example.com")
    await _delete(auth_db_client, token, fresh_flag)
    await _delete(auth_db_client, token, stale_flag)
    await _age_tombstone(db_session, stale_flag, days=30)

    response = await auth_db_client.get("/api/v1/message-flags?deleted=true", headers=_auth(token))

    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [fresh_flag]
    assert live_flag not in ids


async def test_restore_is_404_when_the_conversation_is_deleted(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    # Ancestor liveness is enforced by the shared read scope, so a flag hidden
    # behind a deleted conversation is not restorable on its own.
    caller = await _caller(db_session, flagger_role, email="orphan@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    (message,) = await _messages(db_session, conversation.id, 1)
    created = await _create(
        auth_db_client,
        token,
        {"conversation_id": str(conversation.id), "message_ids": [str(message.id)], "reason": "r"},
    )
    flag_id = created.json()["id"]
    await _delete(auth_db_client, token, flag_id)
    conversation.soft_delete(caller.id)
    db_session.add(conversation)
    await db_session.flush()

    response = await _restore(auth_db_client, token, flag_id)

    _assert_problem(response, status.HTTP_404_NOT_FOUND)


async def test_restore_requires_delete_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    caller = await _caller(db_session, flagger_role, email="perm@example.com")
    token = _token(caller)
    flag_id = await _flagged(auth_db_client, db_session, token, email="perm@example.com")
    await _delete(auth_db_client, token, flag_id)

    reader_role = Role(name="flag-reader", description="read only", permissions=[Permission.FLAGS_READ.value])
    db_session.add(reader_role)
    await db_session.flush()
    reader = await _caller(db_session, reader_role, email="reader@example.com")

    response = await _restore(auth_db_client, _token(reader), flag_id)

    _assert_problem(response, status.HTTP_403_FORBIDDEN)


async def test_flag_embed_carries_the_replys_tag_context(
    auth_db_client: AsyncClient, db_session: AsyncSession, flagger_role: Role
) -> None:
    # The flag embed is the only source of a flagged message a later regenerate superseded, so without
    # this the reviewer's one hard case shows nothing — indistinguishable from "no tags were sent".
    caller = await _caller(db_session, flagger_role, email="tagcontext@example.com")
    token = _token(caller)
    evaluation = await _evaluation_for(db_session)
    conversation = await _conversation(db_session, user_id=caller.id, evaluation=evaluation)
    turn = Turn(conversation_id=conversation.id, turn_index=0)
    db_session.add(turn)
    await db_session.flush()
    reply = Message(
        turn_id=turn.id,
        role=MessageRole.ASSISTANT,
        status=MessageStatus.COMPLETE,
        content="answer",
        extra={TAG_CONTEXT_EXTRA_KEY: {"env": "prod"}},
    )
    db_session.add(reply)
    await db_session.flush()
    await db_session.refresh(reply)
    created = await _create(
        auth_db_client,
        token,
        {"conversation_id": str(conversation.id), "message_ids": [str(reply.id)], "reason": "r"},
    )
    flag_id = created.json()["id"]

    response = await auth_db_client.get(f"/api/v1/message-flags/{flag_id}", headers=_auth(token))

    assert response.json()["messages"][0]["tag_context"] == {"env": "prod"}
