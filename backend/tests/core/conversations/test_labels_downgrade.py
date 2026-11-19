"""Behavior test for the downgrade of the multi-value labels migration (revision `51936c9ff5f4`).

The downgrade deletes rows to fit the old one-value-per-category key, so its SQL is run here
against seeded data; the fixture's outer-transaction rollback undoes it.
"""

from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.conversations.models import ConversationGroup
from app.core.conversations.models import ConversationGroupLabel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationLabelCategory
from app.core.evaluations.models import Scenario
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration

_REVISION = "51936c9ff5f4"


def _migration_module() -> Any:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    return script.get_revision(_REVISION).module


async def test_downgrade_keeps_the_first_value_of_each_category_and_drops_bare_selections(
    db_session: AsyncSession,
) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(title="E", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    scenario = Scenario(name="s", description="d", evaluation_id=evaluation.id, position=0)
    language = EvaluationLabelCategory(evaluation_id=evaluation.id, name="language", values=["english", "polish"])
    marker = EvaluationLabelCategory(evaluation_id=evaluation.id, name="guardrail-violation")
    db_session.add_all([scenario, language, marker])
    await db_session.flush()
    conversation_group = ConversationGroup(
        user_id=group.created_by_id, evaluation_id=evaluation.id, name="g", scenario_id=scenario.id
    )
    db_session.add(conversation_group)
    await db_session.flush()
    db_session.add_all(
        ConversationGroupLabel(conversation_group_id=conversation_group.id, category_id=category_id, value=value)
        for category_id, value in [
            (language.id, "polish"),
            (language.id, "english"),
            (language.id, ""),
            (marker.id, ""),
        ]
    )
    await db_session.flush()

    await db_session.execute(text(_migration_module()._KEEP_ONE_VALUE_PER_CATEGORY))

    rows = await db_session.execute(
        select(col(ConversationGroupLabel.category_id), col(ConversationGroupLabel.value)).where(
            col(ConversationGroupLabel.conversation_group_id) == conversation_group.id
        )
    )
    assert list(rows.tuples()) == [(language.id, "english")]
