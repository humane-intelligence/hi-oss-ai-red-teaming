"""Shared factories for the conversations service tests."""

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import Scenario
from tests.conftest import persist_evaluation_group


async def persist_conversation(db_session: AsyncSession) -> Conversation:
    """Persist a full graph (group → evaluation → model → assignment) plus a conversation owned by its creator."""
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
