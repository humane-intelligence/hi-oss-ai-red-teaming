"""The "reviews" CSV export — one row per `Review` (reviewer verdict) in an evaluation."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.roles import Permission
from app.core.config import get_settings
from app.core.csv_generator import Column
from app.core.exports.base import CsvExport
from app.core.exports.base import ExportScope
from app.core.exports.base import iter_pages
from app.core.exports.filters import coerce_status
from app.core.exports.lookups import resolve_scope_evaluation_title
from app.core.exports.lookups import resolve_user_emails
from app.core.restore import restore_cutoff
from app.core.reviews.enums import ReviewStatus
from app.core.reviews.filters import ReviewFilters
from app.core.reviews.models import Review
from app.core.reviews.services.reviews import list_reviews


@dataclass(frozen=True, slots=True)
class ReviewRow:
    """A review verdict plus the resolved evaluation title and reviewer email."""

    review: Review
    evaluation_title: str | None
    reviewer_email: str | None


async def _fetch_reviews(session: AsyncSession, scope: ExportScope) -> AsyncIterator[ReviewRow]:
    # Authorised exports are group-wide (see ExportScope); reviews read the whole evaluation.
    can_manage = scope.full_group_access
    can_review = scope.full_group_access
    evaluation_title = await resolve_scope_evaluation_title(session, scope)  # constant across the export
    # Reviews honor user (the reviewer) / status / date-range; scenario + task don't apply here.
    ef = scope.filters
    review_filters = ReviewFilters(
        evaluation_id=scope.evaluation_id,
        reviewer_id=ef.user_id,
        status=coerce_status(ReviewStatus, ef.status),
        created_from=ef.created_from,
        created_to=ef.created_to,
    )
    async for reviews in iter_pages(
        lambda limit, offset: list_reviews(
            session,
            caller_id=scope.caller.id,
            can_manage=can_manage,
            can_review=can_review,
            filters=review_filters,
            order_by="-created_at",
            limit=limit,
            offset=offset,
            deleted_cutoff=restore_cutoff(get_settings()),
        )
    ):
        emails = await resolve_user_emails(session, (r.reviewer_id for r in reviews))
        for review in reviews:
            yield ReviewRow(
                review=review,
                evaluation_title=evaluation_title,
                reviewer_email=emails.get(review.reviewer_id),
            )


_COLUMNS: list[Column[ReviewRow]] = [
    Column("Review ID", lambda r: r.review.id),
    Column("Evaluation ID", lambda r: r.review.evaluation_id),
    Column("Evaluation title", lambda r: r.evaluation_title),
    Column("Submission (flag) ID", lambda r: r.review.message_flag_id),
    Column("Reviewer ID", lambda r: r.review.reviewer_id),
    Column("Reviewer email", lambda r: r.reviewer_email),
    Column("Status", lambda r: r.review.status),
    Column("Successful exploit", lambda r: r.review.successful_exploit),
    Column("Unique exploit", lambda r: r.review.unique_exploit),
    Column("Valid submission", lambda r: r.review.valid_submission),
    Column("Notes", lambda r: r.review.notes),
    Column("Created", lambda r: r.review.created_at),
    # `updated_at` moves off `created_at` when the verdict is recorded — so a `pending`
    # row shows when it was assigned, a decided row when the verdict landed.
    Column("Verdict recorded", lambda r: r.review.updated_at),
]


REVIEWS_EXPORT: CsvExport[ReviewRow] = CsvExport(
    key="reviews",
    name="Reviews",
    description=(
        "One row per reviewer verdict in the evaluation — evaluation, submission, reviewer (with email), status, "
        "exploit assessment, notes, and when the verdict was recorded."
    ),
    permission=Permission.REVIEWS_READ,
    columns=_COLUMNS,
    fetch=_fetch_reviews,
    supported_filters=frozenset({"created_from", "created_to", "status", "user_id"}),
)
