"""Behavior tests for the group allowed-model backfill data migration (revision `508387a6dfc1`).

The upgrade only runs once at test-session setup (over empty tables), so its
substantive SQL is exercised here directly against seeded data: DISTINCT live-only
seeding, `NOT EXISTS` idempotency, and fail-closed-empty for zero-usage groups.
"""

import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import TextClause
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import EvaluationGroupAiModel
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration


def _backfill_sql() -> TextClause:
    """Load the migration's `_BACKFILL` statement via Alembic (no fragile file-path import)."""
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    return script.get_revision("508387a6dfc1").module._BACKFILL


async def _persist_model(db_session: AsyncSession, *, alias: str, deleted: bool = False) -> AiModel:
    model = AiModel(name=alias, model_alias=alias, provider=ProviderVendor.ANTHROPIC, provider_model_id=f"{alias}-id")
    db_session.add(model)
    await db_session.flush()
    if deleted:
        model.soft_delete(None)
        await db_session.flush()
    return model


async def _persist_evaluation(
    db_session: AsyncSession, group: EvaluationGroup, *, title: str = "eval", deleted: bool = False
) -> Evaluation:
    evaluation = Evaluation(
        title=title, description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    if deleted:
        evaluation.soft_delete(None)
        await db_session.flush()
    return evaluation


async def _assign(db_session: AsyncSession, evaluation: Evaluation, model: AiModel) -> None:
    db_session.add(EvaluationAiModel(evaluation_id=evaluation.id, model_id=model.id))
    await db_session.flush()


async def _live_subset_ids(db_session: AsyncSession, group_id: uuid.UUID) -> list[uuid.UUID]:
    rows = (
        (
            await db_session.execute(
                EvaluationGroupAiModel.live_select().where(col(EvaluationGroupAiModel.evaluation_group_id) == group_id)
            )
        )
        .scalars()
        .all()
    )
    return [row.model_id for row in rows]


async def test_backfill_seeds_distinct_live_models(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    e1 = await _persist_evaluation(db_session, group, title="e1")
    e2 = await _persist_evaluation(db_session, group, title="e2")
    m1 = await _persist_model(db_session, alias="m1")
    m2 = await _persist_model(db_session, alias="m2")
    await _persist_model(db_session, alias="m3-unused")  # in the registry but never assigned — not seeded
    await _assign(db_session, e1, m1)
    await _assign(db_session, e1, m2)
    await _assign(db_session, e2, m2)  # (group, m2) repeated across evaluations — collapses

    await db_session.execute(_backfill_sql())

    assert set(await _live_subset_ids(db_session, group.id)) == {m1.id, m2.id}


async def test_backfill_is_idempotent(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation(db_session, group)
    model = await _persist_model(db_session, alias="m")
    await _assign(db_session, evaluation, model)

    sql = _backfill_sql()
    await db_session.execute(sql)
    first = await _live_subset_ids(db_session, group.id)
    await db_session.execute(sql)
    second = await _live_subset_ids(db_session, group.id)

    assert first == [model.id]
    assert second == first  # NOT EXISTS guard — the re-run inserts no duplicate row


async def test_backfill_skips_zero_usage_group(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    await _persist_model(db_session, alias="unused")  # registry has a model, the group uses none

    await db_session.execute(_backfill_sql())

    assert await _live_subset_ids(db_session, group.id) == []  # fail-closed


async def test_backfill_skips_soft_deleted_model(db_session: AsyncSession) -> None:
    # A live assignment pointing at a soft-deleted model (a pre-cascade orphan) must not
    # seed a dead model into the subset — the ai_models join filters it out.
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation(db_session, group)
    dead = await _persist_model(db_session, alias="dead", deleted=True)
    await _assign(db_session, evaluation, dead)

    await db_session.execute(_backfill_sql())

    assert await _live_subset_ids(db_session, group.id) == []


async def test_backfill_skips_soft_deleted_evaluation(db_session: AsyncSession) -> None:
    group = await persist_evaluation_group(db_session)
    evaluation = await _persist_evaluation(db_session, group, deleted=True)
    model = await _persist_model(db_session, alias="m")
    await _assign(db_session, evaluation, model)

    await db_session.execute(_backfill_sql())

    assert await _live_subset_ids(db_session, group.id) == []
