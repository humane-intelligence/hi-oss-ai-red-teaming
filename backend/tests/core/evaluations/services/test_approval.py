"""Integration tests for evaluation approval transitions."""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.services.approval import approve_evaluation
from app.core.evaluations.services.approval import reject_evaluation
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.notifications.enums import NotificationObjectType
from app.core.notifications.filters import NotificationFilters
from app.core.notifications.services.notifications import list_notifications
from tests.conftest import persist_evaluation_group

pytestmark = pytest.mark.integration

_NON_REVIEW_STATES = [
    EvaluationStatus.NEW,
    EvaluationStatus.DRAFT,
    EvaluationStatus.APPROVED,
    EvaluationStatus.REJECTED,
    EvaluationStatus.PUBLISHED,
    EvaluationStatus.COMPLETED,
]


async def _persist_evaluation(
    db_session: AsyncSession,
    *,
    status: EvaluationStatus = EvaluationStatus.UNDER_REVIEW,
    rejection_reason: str | None = None,
) -> Evaluation:
    group = await persist_evaluation_group(db_session)
    evaluation = Evaluation(
        title="eval",
        description="d",
        evaluation_group_id=group.id,
        created_by_id=group.created_by_id,
        status=status,
        rejection_reason=rejection_reason,
    )
    db_session.add(evaluation)
    await db_session.flush()
    await db_session.refresh(evaluation)
    return evaluation


async def test_approve_moves_under_review_to_approved_and_clears_reason(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session, rejection_reason="stale note")

    result, previous_status = await approve_evaluation(db_session, evaluation.id, caller_id=uuid4())

    assert result.status is EvaluationStatus.APPROVED
    assert result.rejection_reason is None
    assert previous_status is EvaluationStatus.UNDER_REVIEW


async def test_reject_moves_under_review_to_rejected_with_reason(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)

    result, previous_status = await reject_evaluation(
        db_session, evaluation.id, caller_id=uuid4(), rejection_reason="off-topic"
    )

    assert result.status is EvaluationStatus.REJECTED
    assert result.rejection_reason == "off-topic"
    assert previous_status is EvaluationStatus.UNDER_REVIEW


@pytest.mark.parametrize("status", _NON_REVIEW_STATES)
async def test_approve_rejects_wrong_source_state(db_session: AsyncSession, status: EvaluationStatus) -> None:
    evaluation = await _persist_evaluation(db_session, status=status)

    with pytest.raises(ConflictError):
        await approve_evaluation(db_session, evaluation.id, caller_id=uuid4())


@pytest.mark.parametrize("status", _NON_REVIEW_STATES)
async def test_reject_rejects_wrong_source_state(db_session: AsyncSession, status: EvaluationStatus) -> None:
    evaluation = await _persist_evaluation(db_session, status=status)

    with pytest.raises(ConflictError):
        await reject_evaluation(db_session, evaluation.id, caller_id=uuid4(), rejection_reason="x")


async def _owner_notifications(db_session: AsyncSession, evaluation: Evaluation) -> list:
    items, _ = await list_notifications(
        db_session,
        user_id=evaluation.created_by_id,
        filters=NotificationFilters(),
        order_by="-created_at",
        limit=20,
        offset=0,
    )
    return items


async def test_approve_notifies_owner(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)

    await approve_evaluation(db_session, evaluation.id, caller_id=uuid4())

    (note,) = await _owner_notifications(db_session, evaluation)
    assert note.name == "Evaluation approved"
    assert note.object_type == NotificationObjectType.EVALUATION
    assert note.object_id == evaluation.id
    assert note.read_at is None


async def test_reject_notifies_owner_with_reason(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)

    await reject_evaluation(db_session, evaluation.id, caller_id=uuid4(), rejection_reason="off-topic")

    (note,) = await _owner_notifications(db_session, evaluation)
    assert note.name == "Evaluation rejected"
    assert "off-topic" in note.description
    assert note.object_id == evaluation.id


async def test_approve_by_owner_notifies_nobody(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)

    await approve_evaluation(db_session, evaluation.id, caller_id=evaluation.created_by_id)

    assert await _owner_notifications(db_session, evaluation) == []


async def test_reject_by_owner_notifies_nobody(db_session: AsyncSession) -> None:
    evaluation = await _persist_evaluation(db_session)

    await reject_evaluation(db_session, evaluation.id, caller_id=evaluation.created_by_id, rejection_reason="mine")

    assert await _owner_notifications(db_session, evaluation) == []


async def test_approve_unknown_evaluation_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await approve_evaluation(db_session, uuid4(), caller_id=uuid4())


async def test_reject_unknown_evaluation_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await reject_evaluation(db_session, uuid4(), caller_id=uuid4(), rejection_reason="x")
