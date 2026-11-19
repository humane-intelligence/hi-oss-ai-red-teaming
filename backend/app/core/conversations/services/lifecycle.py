"""Whether an evaluation still takes prompts — the lifecycle gate on starting and continuing conversations."""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.evaluations.enums import EvaluationStatus
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.services.evaluations import may_edit_evaluation
from app.core.exceptions import ConflictError

_CLOSED_EVALUATION_STATUSES = (EvaluationStatus.REJECTED, EvaluationStatus.COMPLETED)


async def assert_accepts_prompts(
    session: AsyncSession, evaluation_id: UUID, *, caller_id: UUID, permissions: Iterable[str]
) -> None:
    """Refuse a new conversation or prompt once the evaluation's group is not open for them.

    A published group takes prompts from anyone who can reach it. Before publication only whoever
    may edit the evaluation does (`may_edit_evaluation`), so a scenario can be tried out before it
    goes live.
    A finished group and a rejected or completed evaluation take none: what was recorded is the
    result. A missing evaluation is left to the caller's own lookup, which owns the 404.

    Raises:
        ConflictError: If the group is finished, the evaluation is closed, or the group is not
            published yet and the caller may not edit the evaluation.
    """
    row = (
        await session.execute(
            select(col(EvaluationGroup.status), col(Evaluation.status))
            .join(Evaluation, col(Evaluation.evaluation_group_id) == col(EvaluationGroup.id))
            .where(col(Evaluation.id) == evaluation_id)
        )
    ).one_or_none()
    if row is None:
        return
    group_status, evaluation_status = row
    if group_status is PublicationStatus.INACTIVE:
        raise ConflictError("This evaluation group is finished, so its conversations no longer take prompts.")
    if evaluation_status in _CLOSED_EVALUATION_STATUSES:
        raise ConflictError("This evaluation is closed, so its conversations no longer take prompts.")
    if group_status is PublicationStatus.PUBLISHED:
        return
    if not await may_edit_evaluation(session, evaluation_id, caller_id=caller_id, permissions=permissions):
        raise ConflictError("This evaluation group is not published yet; conversations can start once it is.")
