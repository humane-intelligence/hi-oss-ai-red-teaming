"""Service-level tests for the conversation-group capacity gate and batch creation.

`acquire_group_for_conversation` is the shared add/move gate: it row-locks the
target group (`for_update`) and rejects once the group is at
`MAX_CONVERSATION_GROUP_SIZE`. The lock's *concurrency* behaviour is a Postgres
row-lock guarantee, not exercised here (the shared `db_session` is a single
connection) — these pin the two things the code itself owns: the locked-resolve
path returns the group, and the cap check raises. Mirrors the `for_update`
smoke test on `get_assignment` (`tests/core/evaluations/services/test_assignments.py`).
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.conversations.services.groups import ConversationSpec
from app.core.conversations.services.groups import acquire_group_for_conversation
from app.core.conversations.services.groups import create_conversation_group
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import Scenario
from app.core.exceptions import ConflictError
from app.core.licenses.models import DataLicense
from tests.conftest import persist_evaluation_group
from tests.core.conversations.services.conftest import persist_conversation

pytestmark = pytest.mark.integration


async def test_acquire_group_for_conversation_returns_locked_group(db_session: AsyncSession) -> None:
    # The seeded group holds one conversation, comfortably under the default cap.
    conversation = await persist_conversation(db_session)

    group = await acquire_group_for_conversation(
        db_session,
        conversation.evaluation_id,
        conversation.conversation_group_id,
        caller_id=conversation.user_id,
    )

    assert group.id == conversation.conversation_group_id


async def test_acquire_group_for_conversation_at_capacity_raises_conflict(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The seeded group already holds one conversation; a cap of 1 makes it full.
    conversation = await persist_conversation(db_session)
    monkeypatch.setattr(
        "app.core.conversations.services.groups.get_settings",
        lambda: SimpleNamespace(max_conversation_group_size=1),
    )

    with pytest.raises(ConflictError):
        await acquire_group_for_conversation(
            db_session,
            conversation.evaluation_id,
            conversation.conversation_group_id,
            caller_id=conversation.user_id,
        )


async def test_batch_created_conversations_inherit_the_licence_protection(db_session: AsyncSession) -> None:
    # This is the path that opens the first conversation of every group, so a conversation missing the
    # flag here writes its whole transcript in the clear under a licence that forbids exactly that.
    lic = DataLicense(
        name=f"Confidential {uuid4().hex[:6]}",
        short_description="No redistribution",
        content="TEXT",
        protects_conversation_data=True,
    )
    db_session.add(lic)
    await db_session.flush()
    group = await persist_evaluation_group(db_session, data_license_id=lic.id)
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
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    db_session.add(scenario)
    await db_session.flush()

    _, conversations = await create_conversation_group(
        db_session,
        user_id=group.created_by_id,
        evaluation_id=evaluation.id,
        scenario_id=scenario.id,
        name="batch",
        models=[ConversationSpec(assignment_id=assignment.id, parameters={}, title=None)],
        can_manage=False,
    )

    # Read the stored row, not the instance: the column is what every later message write consults.
    stored = (
        await db_session.execute(
            text("SELECT content_protected FROM conversations WHERE id = :id"), {"id": conversations[0].id}
        )
    ).scalar_one()
    assert stored is True
