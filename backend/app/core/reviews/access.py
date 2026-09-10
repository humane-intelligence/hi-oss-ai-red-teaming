"""Review read/write scope — visibility spine plus the reviewer-vs-author split.

Reviews are scoped by the parent group's visibility (the shared
`join_visible_evaluation_group`) and a second axis: a **reviewer** (annotator /
owner / admin — anyone holding `reviews:create`) sees every review in a visible
group, while a read-only **red-teamer** sees only reviews of flags they authored.
The `evaluation_groups:manage` break-glass (`can_manage`) lifts both the
visibility and the author predicate. Parent-flag and parent-conversation
liveness are always enforced (a soft-deleted conversation from a model-unassign
cascade hides the submission, mirroring the author-side notes scope).

Writes (assign / verdict / unassign) ride this same visibility spine plus the
JWT `reviews:*` gate — deliberately *not* the in-group write authority
`assert_group_write_access` gates scenarios/tasks with; see
`services.reviews._resolve_assignable_flag` for the rationale.
"""

from uuid import UUID

from sqlalchemy import Select
from sqlmodel import col

from app.core.annotations.models import MessageFlag
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.conversations.models import Conversation
from app.core.evaluations.access import join_visible_evaluation_group
from app.core.reviews.models import Review


def caller_can_review(caller: SessionUser) -> bool:
    """True when the caller holds a reviewer role (annotator / owner / admin).

    Keyed on `reviews:create`, which exactly the reviewer roles carry — it
    distinguishes them from a read-only red-teamer (who sees only their own
    flags' reviews).
    """
    return Permission.REVIEWS_CREATE.value in caller.permissions


def scope_reviews(
    statement: Select[tuple[Review]], *, caller_id: UUID, can_manage: bool, can_review: bool
) -> Select[tuple[Review]]:
    """Constrain a `Review` query to the rows the caller may read.

    Joins the denormalised `evaluation_id` to its live, visible `EvaluationGroup`,
    the parent `MessageFlag` (live), and that flag's live `Conversation` — so a
    soft-deleted conversation hides the review, mirroring the author-side
    notes scope. A reviewer (`can_review`) or break-glass manager
    (`can_manage`) sees every review in a visible group; otherwise the rows are
    narrowed to flags the caller authored.
    """
    statement = join_visible_evaluation_group(
        statement, col(Review.evaluation_id), caller_id=caller_id, can_manage=can_manage
    )
    statement = (
        statement.join(MessageFlag, col(Review.message_flag_id) == col(MessageFlag.id))
        .join(Conversation, col(MessageFlag.conversation_id) == col(Conversation.id))
        .where(col(MessageFlag.deleted_at).is_(None), col(Conversation.deleted_at).is_(None))
    )
    if not can_manage and not can_review:
        statement = statement.where(col(MessageFlag.created_by_id) == caller_id)
    return statement


def scope_review_flags(
    statement: Select[tuple[MessageFlag]], *, caller_id: UUID, can_manage: bool, can_review: bool
) -> Select[tuple[MessageFlag]]:
    """Constrain a `MessageFlag` (queue) query the same way as `scope_reviews`.

    The queue is flag-centric, so the visibility spine joins on the flag's own
    `evaluation_id` and its live `Conversation` (a soft-deleted conversation hides
    the submission, as on the author side); the reviewer / author split is identical.
    """
    statement = join_visible_evaluation_group(
        statement, col(MessageFlag.evaluation_id), caller_id=caller_id, can_manage=can_manage
    )
    statement = statement.join(Conversation, col(MessageFlag.conversation_id) == col(Conversation.id)).where(
        col(Conversation.deleted_at).is_(None)
    )
    if not can_manage and not can_review:
        statement = statement.where(col(MessageFlag.created_by_id) == caller_id)
    return statement
