"""Shared factories for the annotation-domain tests."""

from typing import NamedTuple
from uuid import UUID
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.auth.models import User
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from tests.conftest import persist_evaluation_group


class ConversationTarget(NamedTuple):
    """A live `group → evaluation → conversation → messages` chain to flag, note or complete."""

    group: EvaluationGroup
    evaluation: Evaluation
    conversation: Conversation
    messages: list[Message]


async def persist_user(db_session: AsyncSession) -> User:
    """A throwaway user, for a caller that is deliberately *not* the conversation's owner."""
    user = User(email=f"{uuid4().hex[:8]}@example.com")
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


async def messages_for(
    db_session: AsyncSession,
    conversation_id: UUID,
    *,
    count: int = 1,
    first_turn_index: int = 0,
) -> list[Message]:
    """Seed ``count`` assistant messages, one per turn (the streamed write-path is not exercised here)."""
    messages: list[Message] = []
    for offset in range(count):
        turn = Turn(conversation_id=conversation_id, turn_index=first_turn_index + offset)
        db_session.add(turn)
        await db_session.flush()
        message = Message(
            turn_id=turn.id,
            role=MessageRole.ASSISTANT,
            status=MessageStatus.COMPLETE,
            content=f"reply {first_turn_index + offset}",
        )
        db_session.add(message)
        await db_session.flush()
        await db_session.refresh(message)
        messages.append(message)
    return messages


async def persist_conversation_target(
    db_session: AsyncSession,
    *,
    owner_id: UUID | None = None,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.PUBLIC,
    status: PublicationStatus = PublicationStatus.APPROVED,
    message_count: int = 1,
) -> ConversationTarget:
    """Persist the full chain a create call has to walk, plus the messages to attach to.

    ``owner_id`` owns both the group and the conversation; leave it out to get a
    throwaway owner, which is what the "annotator reaches a conversation they do
    not own" cases need.
    """
    group = await persist_evaluation_group(db_session, access_level=access_level, status=status, created_by_id=owner_id)
    evaluation = Evaluation(
        title="Eval", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    await db_session.refresh(evaluation)
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
        user_id=group.created_by_id, evaluation_id=evaluation.id, name="conversations", scenario_id=scenario.id
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
    await db_session.refresh(conversation)
    messages = await messages_for(db_session, conversation.id, count=message_count)
    return ConversationTarget(group=group, evaluation=evaluation, conversation=conversation, messages=messages)
