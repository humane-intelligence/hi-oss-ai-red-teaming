"""Evaluation approval transitions — admin approve/reject of a pending evaluation.

Only the two admin transitions out of `under_review` are enforced here; the
rest of the evaluation lifecycle (owner submit, publish, close) lands with its
own flows. The allowed-transition table is the guard — a wrong source state is
a 409, not a silent no-op.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.services.assignments import get_evaluation
from app.core.exceptions import ConflictError
from app.core.notifications.enums import NotificationObjectType
from app.core.notifications.services.notifications import create_notification

_APPROVAL_TRANSITIONS: dict[EvaluationStatus, set[EvaluationStatus]] = {
    EvaluationStatus.UNDER_REVIEW: {EvaluationStatus.APPROVED, EvaluationStatus.REJECTED},
}


def _assert_transition(evaluation: Evaluation, target: EvaluationStatus) -> None:
    if target not in _APPROVAL_TRANSITIONS.get(evaluation.status, set()):
        raise ConflictError(f"Cannot move evaluation {evaluation.id} from '{evaluation.status}' to '{target}'.")


async def approve_evaluation(
    session: AsyncSession, evaluation_id: UUID, *, caller_id: UUID
) -> tuple[Evaluation, EvaluationStatus]:
    """Approve an `under_review` evaluation, clearing any stale rejection reason.

    Returns the approved evaluation and its **prior** status (for the audit `before`), read
    off this locked row — so the caller must not re-read it unlocked: a second read would
    load a stale identity-map copy and defeat this `FOR UPDATE`.

    Notifies the owner unless the caller is the owner (self-verdict is not news) — admin
    holds both `evaluations:create` and `evaluations:approve`, so it can approve its own.

    Raises:
        NotFoundError: If no live evaluation matches ``evaluation_id``.
        ConflictError: If the evaluation is not `under_review`.
    """
    evaluation = await get_evaluation(session, evaluation_id, for_update=True)
    previous_status = evaluation.status
    _assert_transition(evaluation, EvaluationStatus.APPROVED)
    evaluation.status = EvaluationStatus.APPROVED
    evaluation.rejection_reason = None
    session.add(evaluation)
    await session.flush()
    await session.refresh(evaluation, attribute_names=["updated_at"])
    if caller_id != evaluation.created_by_id:
        await create_notification(
            session,
            user_id=evaluation.created_by_id,
            name="Evaluation approved",
            description=f"Your evaluation '{evaluation.title}' was approved.",
            object_type=NotificationObjectType.EVALUATION,
            object_id=evaluation.id,
        )
    return evaluation, previous_status


async def reject_evaluation(
    session: AsyncSession, evaluation_id: UUID, *, caller_id: UUID, rejection_reason: str
) -> tuple[Evaluation, EvaluationStatus]:
    """Reject an `under_review` evaluation, recording the reason.

    Returns the rejected evaluation and its **prior** status (for the audit `before`), read
    off this locked row — so the caller must not re-read it unlocked (see `approve_evaluation`).

    Notifies the owner unless the caller is the owner (see `approve_evaluation`).

    Args:
        session: Async DB session bound to the request.
        evaluation_id: Evaluation to reject.
        caller_id: Acting user; the owner-notify is skipped when it is the owner.
        rejection_reason: Why it was rejected; non-empty is enforced at the edge.

    Raises:
        NotFoundError: If no live evaluation matches ``evaluation_id``.
        ConflictError: If the evaluation is not `under_review`.
    """
    evaluation = await get_evaluation(session, evaluation_id, for_update=True)
    previous_status = evaluation.status
    _assert_transition(evaluation, EvaluationStatus.REJECTED)
    evaluation.status = EvaluationStatus.REJECTED
    evaluation.rejection_reason = rejection_reason
    session.add(evaluation)
    await session.flush()
    await session.refresh(evaluation, attribute_names=["updated_at"])
    if caller_id != evaluation.created_by_id:
        await create_notification(
            session,
            user_id=evaluation.created_by_id,
            name="Evaluation rejected",
            description=f"Your evaluation '{evaluation.title}' was rejected: {rejection_reason}",
            object_type=NotificationObjectType.EVALUATION,
            object_id=evaluation.id,
        )
    return evaluation, previous_status
