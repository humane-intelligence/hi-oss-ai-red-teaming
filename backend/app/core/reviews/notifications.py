"""Best-effort reviewer notifications for (un)assignment.

Post-conditions of the assign / unassign write — the review row is already
flushed, so a mail failure must not roll it back; `send_email_best_effort`
confines the failure to a SAVEPOINT.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import User
from app.core.auth.schemas import SessionUser
from app.core.email import send_email_best_effort
from app.core.evaluations.models import Evaluation
from app.core.reviews.models import Review


async def notify_review_assigned(session: AsyncSession, review: Review, *, actor: SessionUser) -> None:
    """Notify the reviewer they were assigned to `review`'s flagged submission."""
    await _notify(session, review, template_name="review_assigned", actor=actor)


async def notify_review_unassigned(session: AsyncSession, review: Review, *, actor: SessionUser) -> None:
    """Notify the reviewer they were unassigned from `review`'s flagged submission."""
    await _notify(session, review, template_name="review_unassigned", actor=actor)


async def _notify(session: AsyncSession, review: Review, *, template_name: str, actor: SessionUser) -> None:
    reviewer = (
        await session.execute(User.live_select().where(col(User.id) == review.reviewer_id))
    ).scalar_one_or_none()
    if reviewer is None:
        return
    evaluation = (
        await session.execute(Evaluation.live_select().where(col(Evaluation.id) == review.evaluation_id))
    ).scalar_one_or_none()
    # TODO: add a review_url deep-link to the context (and the templates) once the
    # frontend has a review page to point at — omitted now as there's no route to link to.
    await send_email_best_effort(
        session,
        template_name,
        reviewer.email,
        {
            "assignee_name": reviewer.display_name,
            "assigner_name": actor.display_name,
            "evaluation_title": evaluation.title if evaluation is not None else "",
        },
    )
