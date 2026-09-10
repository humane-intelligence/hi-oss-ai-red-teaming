"""Service-level tests for conversation dispatch resolution.

`resolve_dispatch_target` is where the per-conversation parameter override earns
its keep: it wires the model → assignment → conversation cascade into the params
that actually drive generation. `merge_inference_params` is unit-tested in
`tests/core/ai_gateway/test_inference_params.py`; this pins the DB-backed wiring.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.conversations.services.conversations import create_conversation
from app.core.conversations.services.conversations import resolve_dispatch_target
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import Scenario
from app.core.licenses.models import DataLicense
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


async def test_resolve_dispatch_target_conversation_params_win_over_assignment_and_model(
    db_session: AsyncSession,
) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(
        title="Eval",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        mask_models_enabled=False,
    )
    db_session.add(evaluation)
    await db_session.flush()
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(
        name=alias,
        model_alias=alias,
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id=f"{alias}-id",
        parameters={"temperature": 0.1, "max_tokens": 1024},
    )
    db_session.add(model)
    await db_session.flush()
    assignment = EvaluationAiModel(
        evaluation_id=evaluation.id, model_id=model.id, parameters={"temperature": 0.5, "top_p": 0.9}
    )
    db_session.add(assignment)
    await db_session.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()
    conversation_group = ConversationGroup(
        user_id=group.created_by_id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id
    )
    db_session.add(conversation_group)
    await db_session.flush()
    conversation = Conversation(
        user_id=group.created_by_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        conversation_group_id=conversation_group.id,
        scenario_id=scenario.id,
        parameters={"temperature": 0.9},
    )
    db_session.add(conversation)
    await db_session.flush()

    resolved_alias, params, mask = await resolve_dispatch_target(db_session, conversation)

    assert resolved_alias == alias
    # conversation wins on temperature; top_p inherited from assignment, max_tokens from model.
    assert params == {"temperature": 0.9, "top_p": 0.9, "max_tokens": 1024}
    assert mask is False


async def _protecting_licence(db_session: AsyncSession) -> DataLicense:
    lic = DataLicense(
        name=f"Confidential {uuid4().hex[:6]}",
        short_description="No redistribution",
        content="TEXT",
        protects_conversation_data=True,
    )
    db_session.add(lic)
    await db_session.flush()
    return lic


async def _graph(db_session: AsyncSession, *, group_license_id=None, evaluation_license_id=None):
    """Group → evaluation → model → assignment → conversation group, ready for `create_conversation`."""
    group = await persist_evaluation_group(db_session, data_license_id=group_license_id)
    evaluation = Evaluation(
        title="Eval",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        data_license_id=evaluation_license_id,
    )
    db_session.add(evaluation)
    await db_session.flush()
    alias = f"m-{uuid4().hex[:8]}"
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    assignment = EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id)
    db_session.add(assignment)
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()
    conversation_group = ConversationGroup(
        user_id=group.created_by_id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id
    )
    db_session.add(conversation_group)
    await db_session.flush()
    return group, evaluation, assignment, conversation_group


async def _stored_protection(db_session: AsyncSession, conversation_id) -> bool:
    """Read the column back, not the instance: the write path consults the stored row."""
    return (
        await db_session.execute(
            text("SELECT content_protected FROM conversations WHERE id = :id"), {"id": conversation_id}
        )
    ).scalar_one()


async def test_conversation_under_a_protecting_licence_is_marked_protected(db_session: AsyncSession) -> None:
    # The decision is denormalised onto the row at creation: message writes read it instead of walking
    # the group → evaluation → licence cascade on the streaming path.
    lic = await _protecting_licence(db_session)
    group, evaluation, assignment, conversation_group = await _graph(db_session, evaluation_license_id=lic.id)

    conversation = await create_conversation(
        db_session,
        user_id=group.created_by_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        scenario_id=conversation_group.scenario_id,
        parameters={},
        conversation_group_id=conversation_group.id,
    )

    assert await _stored_protection(db_session, conversation.id) is True


async def test_conversation_inherits_the_protection_from_the_group(db_session: AsyncSession) -> None:
    # The evaluation carries no licence of its own, so the cascade has to reach the group's.
    lic = await _protecting_licence(db_session)
    group, evaluation, assignment, conversation_group = await _graph(db_session, group_license_id=lic.id)

    conversation = await create_conversation(
        db_session,
        user_id=group.created_by_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        scenario_id=conversation_group.scenario_id,
        parameters={},
        conversation_group_id=conversation_group.id,
    )

    assert await _stored_protection(db_session, conversation.id) is True


async def test_conversation_under_an_unprotecting_licence_stays_plain(db_session: AsyncSession) -> None:
    group, evaluation, assignment, conversation_group = await _graph(db_session)

    conversation = await create_conversation(
        db_session,
        user_id=group.created_by_id,
        evaluation_id=evaluation.id,
        evaluation_ai_model_id=assignment.id,
        scenario_id=conversation_group.scenario_id,
        parameters={},
        conversation_group_id=conversation_group.id,
    )

    assert await _stored_protection(db_session, conversation.id) is False
