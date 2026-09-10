"""Model-level tests for `Turn` and `Message` — FK enforcement, cascade, and the
dense-index / idempotency constraints the write-path relies on."""

from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import Scenario
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


async def _persist_conversation(db_session: AsyncSession) -> Conversation:
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(
        title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    db_session.add(assignment)
    await db_session.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()
    conversation_group = ConversationGroup(
        user_id=group.created_by_id, evaluation_id=evaluation.id, name="group", scenario_id=scenario.id
    )
    db_session.add(conversation_group)
    await db_session.flush()
    conversation = Conversation(
        user_id=group.created_by_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=scenario.id,
    )
    db_session.add(conversation)
    await db_session.flush()
    return conversation


async def _persist_turn(db_session: AsyncSession, conversation: Conversation, *, turn_index: int = 0) -> Turn:
    turn = Turn(conversation_id=conversation.id, turn_index=turn_index)
    db_session.add(turn)
    await db_session.flush()
    return turn


async def test_content_protection_columns_default_to_off(db_session: AsyncSession) -> None:
    # Every conversation and message written before this feature must read exactly as it did:
    # plaintext, unprotected. The pair is opt-in, and the opt-in comes from the licence.
    conversation = await _persist_conversation(db_session)
    turn = await _persist_turn(db_session, conversation)
    message = Message(turn_id=turn.id, role=MessageRole.USER, status=MessageStatus.COMPLETE, content="hello")
    db_session.add(message)
    await db_session.flush()

    await db_session.refresh(conversation)
    await db_session.refresh(message)

    assert conversation.content_protected is False
    assert message.content_encrypted is False


async def test_content_protection_columns_have_a_server_default(db_session: AsyncSession) -> None:
    # The ORM always sends both columns, so only a write that omits them can tell whether the
    # migration's `server_default` is there — and without it the `NOT NULL` breaks every pre-existing
    # row on upgrade, which is the whole back-compat claim of this change.
    conversation = await _persist_conversation(db_session)
    turn = await _persist_turn(db_session, conversation)

    await db_session.execute(
        text("INSERT INTO messages (id, turn_id, role, status, content) VALUES (:id, :turn, 'user', 'complete', 'x')"),
        {"id": uuid4(), "turn": turn.id},
    )

    await db_session.execute(
        text(
            "INSERT INTO conversations "
            "(id, user_id, evaluation_id, evaluation_ai_model_id, conversation_group_id, scenario_id) "
            "SELECT :id, user_id, evaluation_id, evaluation_ai_model_id, conversation_group_id, scenario_id "
            "FROM conversations WHERE id = :source"
        ),
        {"id": uuid4(), "source": conversation.id},
    )

    stored = (await db_session.execute(text("SELECT content_encrypted FROM messages WHERE content = 'x'"))).scalar_one()
    assert stored is False
    unprotected = (
        await db_session.execute(
            text("SELECT bool_and(NOT content_protected) FROM conversations WHERE evaluation_id = :evaluation"),
            {"evaluation": conversation.evaluation_id},
        )
    ).scalar_one()
    assert unprotected is True


# --- Turn ---------------------------------------------------------------------


async def test_turn_persists_fields(db_session: AsyncSession) -> None:
    conversation = await _persist_conversation(db_session)

    turn = await _persist_turn(db_session, conversation, turn_index=0)
    await db_session.refresh(turn)

    assert turn.id is not None
    assert turn.conversation_id == conversation.id
    assert turn.turn_index == 0
    assert turn.deleted_at is None


async def test_turn_requires_existing_conversation(db_session: AsyncSession) -> None:
    turn = Turn(conversation_id=uuid4(), turn_index=0)
    db_session.add(turn)

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_turn_index_unique_within_conversation(db_session: AsyncSession) -> None:
    conversation = await _persist_conversation(db_session)
    await _persist_turn(db_session, conversation, turn_index=0)

    db_session.add(Turn(conversation_id=conversation.id, turn_index=0))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_turn_index_may_repeat_across_conversations(db_session: AsyncSession) -> None:
    first = await _persist_conversation(db_session)
    second = await _persist_conversation(db_session)

    await _persist_turn(db_session, first, turn_index=0)
    await _persist_turn(db_session, second, turn_index=0)  # same index, different conversation — fine

    turns = (await db_session.execute(select(Turn).where(col(Turn.turn_index) == 0))).scalars().all()
    assert {t.conversation_id for t in turns} == {first.id, second.id}


# --- Message ------------------------------------------------------------------


async def test_message_persists_fields(db_session: AsyncSession) -> None:
    conversation = await _persist_conversation(db_session)
    turn = await _persist_turn(db_session, conversation)

    message = Message(
        turn_id=turn.id,
        role=MessageRole.USER,
        status=MessageStatus.COMPLETE,
        content="hi",
        client_message_id=uuid4(),
    )
    db_session.add(message)
    await db_session.flush()
    await db_session.refresh(message)

    assert message.role is MessageRole.USER
    assert message.status is MessageStatus.COMPLETE
    assert message.content == "hi"
    assert message.replaces_message_id is None


async def test_assistant_placeholder_defaults_to_empty_content(db_session: AsyncSession) -> None:
    conversation = await _persist_conversation(db_session)
    turn = await _persist_turn(db_session, conversation)

    placeholder = Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.STREAMING, slot="a")
    db_session.add(placeholder)
    await db_session.flush()
    await db_session.refresh(placeholder)

    assert placeholder.content == ""
    assert placeholder.slot == "a"
    assert placeholder.extra == {}  # generation metadata grab-bag defaults empty, never null


async def test_message_requires_existing_turn(db_session: AsyncSession) -> None:
    message = Message(turn_id=uuid4(), role=MessageRole.USER, status=MessageStatus.COMPLETE, content="x")
    db_session.add(message)

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_client_message_id_unique_when_present(db_session: AsyncSession) -> None:
    conversation = await _persist_conversation(db_session)
    turn = await _persist_turn(db_session, conversation)
    client_id = uuid4()
    db_session.add(
        Message(turn_id=turn.id, role=MessageRole.USER, status=MessageStatus.COMPLETE, client_message_id=client_id)
    )
    await db_session.flush()

    db_session.add(
        Message(turn_id=turn.id, role=MessageRole.USER, status=MessageStatus.COMPLETE, client_message_id=client_id)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_null_client_message_id_is_not_constrained(db_session: AsyncSession) -> None:
    # Partial unique (WHERE client_message_id IS NOT NULL) must let many null ids coexist.
    conversation = await _persist_conversation(db_session)
    turn = await _persist_turn(db_session, conversation)

    db_session.add_all(
        [
            Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.STREAMING, slot="a"),
            Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.STREAMING, slot="b"),
        ]
    )

    await db_session.flush()  # no IntegrityError


async def test_replaces_message_id_set_null_on_hard_delete(db_session: AsyncSession) -> None:
    conversation = await _persist_conversation(db_session)
    turn = await _persist_turn(db_session, conversation)
    old = Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content="v1")
    db_session.add(old)
    await db_session.flush()
    new = Message(
        turn_id=turn.id,
        role=MessageRole.ASSISTANT,
        status=MessageStatus.COMPLETE,
        content="v2",
        replaces_message_id=old.id,
    )
    db_session.add(new)
    await db_session.flush()

    await db_session.execute(delete(Message).where(col(Message.id) == old.id))
    await db_session.refresh(new)

    assert new.replaces_message_id is None


async def test_replaces_message_id_allows_at_most_one_successor(db_session: AsyncSession) -> None:
    # Linear history: two messages cannot both supersede the same one (partial-unique).
    conversation = await _persist_conversation(db_session)
    turn = await _persist_turn(db_session, conversation)
    base = Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, content="v1")
    db_session.add(base)
    await db_session.flush()
    db_session.add(
        Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, replaces_message_id=base.id)
    )
    await db_session.flush()

    db_session.add(
        Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.COMPLETE, replaces_message_id=base.id)
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


# --- Cascade ------------------------------------------------------------------


async def test_deleting_turn_cascades_messages(db_session: AsyncSession) -> None:
    conversation = await _persist_conversation(db_session)
    turn = await _persist_turn(db_session, conversation)
    db_session.add(Message(turn_id=turn.id, role=MessageRole.USER, status=MessageStatus.COMPLETE, content="x"))
    await db_session.flush()

    await db_session.execute(delete(Turn).where(col(Turn.id) == turn.id))

    remaining = (await db_session.execute(select(Message).where(col(Message.turn_id) == turn.id))).scalars().all()
    assert remaining == []


async def test_deleting_conversation_cascades_to_turns_and_messages(db_session: AsyncSession) -> None:
    # Transitive: conversation → turns → messages, all via DB ON DELETE CASCADE.
    conversation = await _persist_conversation(db_session)
    turn = await _persist_turn(db_session, conversation)
    db_session.add(Message(turn_id=turn.id, role=MessageRole.USER, status=MessageStatus.COMPLETE, content="x"))
    await db_session.flush()

    await db_session.execute(delete(Conversation).where(col(Conversation.id) == conversation.id))

    turns = (await db_session.execute(select(Turn).where(col(Turn.conversation_id) == conversation.id))).scalars().all()
    messages = (await db_session.execute(select(Message).where(col(Message.turn_id) == turn.id))).scalars().all()
    assert turns == []
    assert messages == []
