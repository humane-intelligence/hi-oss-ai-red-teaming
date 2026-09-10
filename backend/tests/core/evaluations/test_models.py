"""Integration tests for `Evaluation` / `EvaluationAiModel` DB constraints and defaults."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from tests.conftest import persist_evaluation_group


async def _persist_model(db_session: AsyncSession, *, model_alias: str = "assigned") -> AiModel:
    """Flush an `AiModel` so its PK is available for an `EvaluationAiModel.model_id` FK."""
    model = AiModel(
        name=model_alias,
        model_alias=model_alias,
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id="claude-3-5-sonnet-20240620",
    )
    db_session.add(model)
    await db_session.flush()
    return model


async def _persist_evaluation(db_session: AsyncSession, *, title: str = "eval") -> Evaluation:
    """Flush an `Evaluation` so its PK is available for an `EvaluationAiModel.evaluation_id` FK."""
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(
        title=title, description="desc", evaluation_group_id=group.id, created_by_id=group.created_by_id
    )
    db_session.add(evaluation)
    await db_session.flush()
    return evaluation


@pytest.mark.integration
async def test_evaluation_server_defaults(db_session: AsyncSession) -> None:
    """A minimal evaluation leans on the DB defaults for status and the masking toggle."""
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    await db_session.refresh(evaluation)

    assert evaluation.status is EvaluationStatus.NEW
    assert evaluation.mask_models_enabled is True
    assert evaluation.rejection_reason is None
    assert evaluation.cover_image is None


@pytest.mark.integration
async def test_evaluation_models_relationship_loads_assignments(db_session: AsyncSession) -> None:
    model = await _persist_model(db_session)
    evaluation = await _persist_evaluation(db_session)
    db_session.add(EvaluationAiModel(model_id=model.id, evaluation_id=evaluation.id))
    await db_session.flush()
    await db_session.refresh(evaluation, attribute_names=["models"])

    assert [assignment.model_id for assignment in evaluation.models] == [model.id]


@pytest.mark.integration
async def test_assignment_server_defaults_populate_parameters(db_session: AsyncSession) -> None:
    model = await _persist_model(db_session)
    evaluation = await _persist_evaluation(db_session)
    assignment = EvaluationAiModel(model_id=model.id, evaluation_id=evaluation.id)
    db_session.add(assignment)
    await db_session.flush()
    await db_session.refresh(assignment)

    assert assignment.parameters == {}
    assert assignment.model_display_mask is None


@pytest.mark.integration
async def test_assignment_partial_unique_blocks_duplicate_live_pair(db_session: AsyncSession) -> None:
    model = await _persist_model(db_session)
    evaluation = await _persist_evaluation(db_session)
    db_session.add(EvaluationAiModel(model_id=model.id, evaluation_id=evaluation.id))
    await db_session.flush()
    db_session.add(EvaluationAiModel(model_id=model.id, evaluation_id=evaluation.id))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
async def test_assignment_partial_unique_allows_reassignment_after_soft_delete(db_session: AsyncSession) -> None:
    model = await _persist_model(db_session)
    evaluation = await _persist_evaluation(db_session)
    first = EvaluationAiModel(model_id=model.id, evaluation_id=evaluation.id)
    db_session.add(first)
    await db_session.flush()
    first.soft_delete(None)
    await db_session.flush()

    db_session.add(EvaluationAiModel(model_id=model.id, evaluation_id=evaluation.id))
    await db_session.flush()  # no IntegrityError — soft-deleted row is invisible to the partial index


@pytest.mark.integration
async def test_assignment_allows_same_model_across_distinct_evaluations(db_session: AsyncSession) -> None:
    model = await _persist_model(db_session)
    first = await _persist_evaluation(db_session, title="a")
    second = await _persist_evaluation(db_session, title="b")
    db_session.add(EvaluationAiModel(model_id=model.id, evaluation_id=first.id))
    db_session.add(EvaluationAiModel(model_id=model.id, evaluation_id=second.id))

    await db_session.flush()  # uniqueness is on the (model_id, evaluation_id) pair, not model_id alone


@pytest.mark.integration
async def test_assignment_foreign_key_rejects_unknown_model(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)
    db_session.add(EvaluationAiModel(model_id=uuid.uuid4(), evaluation_id=evaluation.id))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
async def test_assignment_foreign_key_rejects_unknown_evaluation(db_session: AsyncSession) -> None:
    model = await _persist_model(db_session)
    db_session.add(EvaluationAiModel(model_id=model.id, evaluation_id=uuid.uuid4()))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
async def test_deleting_group_cascades_to_evaluations(db_session: AsyncSession) -> None:
    """A hard delete of the parent group cascades to its evaluations (ON DELETE CASCADE)."""
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(title="t", description="d", evaluation_group_id=group.id, created_by_id=group.created_by_id)
    db_session.add(evaluation)
    await db_session.flush()
    evaluation_id = evaluation.id

    await db_session.delete(group)
    await db_session.flush()

    remaining = (
        await db_session.execute(select(Evaluation).where(col(Evaluation.id) == evaluation_id))
    ).scalar_one_or_none()
    assert remaining is None
