"""Review endpoints — a flat `/reviews` resource plus the `/review-queue` dashboard.

A review is a reviewer's verdict on a flagged submission (`MessageFlag`). Assign
a reviewer (`POST /reviews`), record the verdict (`PATCH /reviews/{id}`), or
unassign (`DELETE`); these gate on `reviews:{create,update,delete}` and are held
by annotator / owner / admin. Reads (`GET /reviews`, `/reviews/{id}`,
`/review-queue`, `/submissions/{submission_id}`, `/submissions/{submission_id}/messages`) gate on `reviews:read`:
a reviewer (annotator / owner / admin) sees every review in a visible group, a read-only red-teamer only reviews of
flags they authored. The `evaluation_groups:manage` break-glass lifts the scope.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Response
from fastapi import status

from app.core.annotations.schemas import MessageFlagResponse
from app.core.audit.enums import AuditAction
from app.core.audit.service import changed_fields
from app.core.audit.service import record_audit
from app.core.auth.dependencies import require_permission
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.bulk import BulkResponse
from app.core.bulk import apply_bulk
from app.core.conversations.schemas import TranscriptMessage
from app.core.conversations.services.messages import image_keys_for_messages
from app.core.dependencies import DbSession
from app.core.dependencies import PaginationDep
from app.core.dependencies import SettingsDep
from app.core.dependencies import transactional
from app.core.evaluations.access import caller_can_manage_groups as _can_manage
from app.core.exceptions import ForbiddenError
from app.core.logging import get_logger
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.openapi import problem_response
from app.core.restore import DeletedFilter
from app.core.restore import restore_cutoff
from app.core.reviews.access import caller_can_review as _can_review
from app.core.reviews.dependencies import ReviewFiltersDep
from app.core.reviews.dependencies import ReviewQueueFiltersDep
from app.core.reviews.enums import ReviewStatus
from app.core.reviews.filters import ReviewOrderBy
from app.core.reviews.models import Review
from app.core.reviews.notifications import notify_review_assigned
from app.core.reviews.notifications import notify_review_unassigned
from app.core.reviews.schemas import AssignableReviewerResponse
from app.core.reviews.schemas import ReviewBulkRequest
from app.core.reviews.schemas import ReviewCreate
from app.core.reviews.schemas import ReviewQueueItem
from app.core.reviews.schemas import ReviewResponse
from app.core.reviews.schemas import ReviewUpdate
from app.core.reviews.schemas import ReviewUpdateChanges
from app.core.reviews.schemas import SubmissionDetailResponse
from app.core.reviews.services.reviews import assert_review_reassignable
from app.core.reviews.services.reviews import assign_reviewer
from app.core.reviews.services.reviews import count_active_reviews
from app.core.reviews.services.reviews import get_restorable_review
from app.core.reviews.services.reviews import get_review
from app.core.reviews.services.reviews import get_submission_detail
from app.core.reviews.services.reviews import list_assignable_reviewers
from app.core.reviews.services.reviews import list_reviews
from app.core.reviews.services.reviews import project_review
from app.core.reviews.services.reviews import project_reviews
from app.core.reviews.services.reviews import resolve_reviewer_emails
from app.core.reviews.services.reviews import restore_review
from app.core.reviews.services.reviews import review_queue
from app.core.reviews.services.reviews import submission_conversation_messages
from app.core.reviews.services.reviews import unassign_review
from app.core.reviews.services.reviews import update_review
from app.core.schemas import Page

router = APIRouter(tags=["reviews"])

logger = get_logger(__name__)

_UNAUTHORIZED = problem_response("Missing or invalid bearer token.")
_FORBIDDEN = problem_response("Caller lacks the required permission.")
_REVIEW_NOT_FOUND = problem_response("Review does not exist or is not visible to the caller.")
_ASSIGN_NOT_FOUND = problem_response(
    "Flag does not exist (or is not visible), or the reviewer is not in its assignable-reviewer pool."
)
_ASSIGN_CONFLICT = problem_response(
    "Submission is already decided, the reviewer is already assigned, or is the submission's own author (self-review)."
)
_VERDICT_FORBIDDEN = problem_response(
    "Caller lacks `reviews:update`, or is not the assigned reviewer (nor a break-glass manager)."
)
_UNASSIGN_FORBIDDEN = problem_response(
    "Caller lacks `reviews:delete`, or may not unassign a decided review they did not record (and isn't a manager)."
)
_RESTORE_FORBIDDEN = problem_response(
    "Caller lacks `reviews:delete`, or may not restore a decided review they did not record (and isn't a manager)."
)
_NOT_RESTORABLE = problem_response(
    "No restorable review with this id: never unassigned, unassigned longer than the restore window ago, "
    "unassigned by someone else, not visible to the caller, or (for a pending review) its reviewer is no "
    "longer live or assignable."
)
_SUBMISSION_NOT_FOUND = problem_response("Submission does not exist or is not visible to the caller.")

_AssignCaller = Annotated[SessionUser, Depends(require_permission(Permission.REVIEWS_CREATE))]
_ReadCaller = Annotated[SessionUser, Depends(require_permission(Permission.REVIEWS_READ))]
_UpdateCaller = Annotated[SessionUser, Depends(require_permission(Permission.REVIEWS_UPDATE))]
_DeleteCaller = Annotated[SessionUser, Depends(require_permission(Permission.REVIEWS_DELETE))]

_OrderBy = Annotated[
    ReviewOrderBy,
    Query(description="Column to order by; prefix with `-` for descending."),
]


def _review_snapshot(review: Review) -> dict[str, object]:
    """Curated snapshot for the audit before/after — assignment target + verdict fields."""
    return {
        "reviewer_id": str(review.reviewer_id),
        "message_flag_id": str(review.message_flag_id),
        "status": str(review.status),
        "successful_exploit": review.successful_exploit,
        "unique_exploit": review.unique_exploit,
        "valid_submission": review.valid_submission,
        "number_prompts": review.number_prompts,
        "notes": review.notes,
    }


@router.post(
    "/reviews",
    response_model=ReviewResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a reviewer",
    description=(
        "Assign a reviewer to a flagged submission — creates a `pending` review. Rejects an already-decided "
        "submission, the same reviewer twice, or the submission's own author."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _ASSIGN_NOT_FOUND,
        status.HTTP_409_CONFLICT: _ASSIGN_CONFLICT,
    },
)
@transactional
async def assign_reviewer_endpoint(
    payload: ReviewCreate,
    caller: _AssignCaller,
    db: DbSession,
    response: Response,
) -> ReviewResponse:
    """Assign a reviewer to a flagged submission.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `reviews:create`.
    * **404 Not Found** — the flag isn't visible to the caller, or the reviewer isn't in its assignable-reviewer pool.
    * **409 Conflict** — the submission is already decided, or the reviewer is already assigned / its own author.
    """
    review = await assign_reviewer(
        db,
        payload.message_flag_id,
        reviewer_id=payload.reviewer_id,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
    )
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.REVIEW_ASSIGN,
        object_type="review",
        object_id=review.id,
        after=_review_snapshot(review),
    )
    await notify_review_assigned(db, review, actor=caller)
    response.headers["Location"] = f"/api/v1/reviews/{review.id}"
    return await project_review(db, review)


@router.post(
    "/reviews/bulk",
    response_model=BulkResponse[ReviewResponse],
    summary="Assign reviewers in bulk",
    description=(
        "Assign many reviewers to many flags in one request. Each row is one "
        "`{message_flag_id, reviewer_id}` pair, so a flags-by-reviewers cartesian goes in one call. "
        "Per-row failures (already-assigned, self-author, non-pending, non-assignable) are reported "
        "in `results` without failing the batch. `dry_run` previews without committing. Each successful "
        "assignment notifies the reviewer."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def bulk_assign_reviewers_endpoint(
    payload: ReviewBulkRequest,
    caller: _AssignCaller,
    db: DbSession,
) -> BulkResponse[ReviewResponse]:
    """Assign reviewers to flagged submissions in bulk.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `reviews:create`.
    * **422 Unprocessable Content** — duplicate `row_key` or over the bulk row limit.

    Per-row `404`/`409` outcomes are carried in `results[].error`, not raised.
    """
    assigned: list[Review] = []

    async def _process(session: DbSession, data: ReviewCreate) -> ReviewResponse:
        review = await assign_reviewer(
            session,
            data.message_flag_id,
            reviewer_id=data.reviewer_id,
            caller_id=caller.id,
            can_manage=_can_manage(caller),
        )
        if not payload.dry_run:
            await record_audit(
                session,
                actor_id=caller.id,
                actor_email=caller.email,
                action=AuditAction.REVIEW_ASSIGN,
                object_type="review",
                object_id=review.id,
                after=_review_snapshot(review),
            )
            assigned.append(review)
        return ReviewResponse.from_model(review)

    result = await apply_bulk(db, payload, _process)

    # Best-effort post-commit notify: assignments are already durable (apply_bulk committed), so a
    # notify/commit failure is logged, not raised. Reuses the single-assign mailer — N+1 SELECTs + one
    # apply_async per row before this single commit; fine for the one-flag, many-reviewer UI, but the
    # same known large-batch mail race the invitations-bulk route documents.
    if not payload.dry_run and assigned:
        try:
            for review in assigned:
                await notify_review_assigned(db, review, actor=caller)
            await db.commit()
        except Exception:
            logger.exception("bulk_assign.notify_failed", assigned=len(assigned))

    return result


@router.get(
    "/reviews",
    response_model=Page[ReviewResponse],
    status_code=status.HTTP_200_OK,
    summary="List reviews",
    description=(
        "Paginated list of reviews the caller may read; filter by `message_flag_id` (one submission's reviews), "
        "`evaluation_id` (all reviews of an evaluation), `reviewer_id`, and/or `status`. Most recent first. "
        "Pass `deleted=true` for unassigned reviews still inside the restore window — the set "
        "`POST /reviews/{review_id}/restore` accepts."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def list_reviews_endpoint(
    caller: _ReadCaller,
    pagination: PaginationDep,
    filters: ReviewFiltersDep,
    db: DbSession,
    settings: SettingsDep,
    order_by: _OrderBy = "-created_at",
    deleted: DeletedFilter = False,
) -> Page[ReviewResponse]:
    """List reviews visible to the caller.

    A red-teamer sees only reviews of flags they authored; a reviewer (annotator /
    owner / admin) sees every review in a visible group. Filters can only narrow.

    `deleted=true` needs no permission of its own: it returns the caller's own
    unassignments (a break-glass manager's, every actor's), so a caller who has never
    unassigned anything gets an empty page rather than a 403.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `reviews:read`.
    """
    reviews, total = await list_reviews(
        db,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        can_review=_can_review(caller),
        filters=filters,
        order_by=order_by,
        limit=pagination.limit,
        offset=pagination.offset,
        deleted=deleted,
        deleted_cutoff=restore_cutoff(settings),
    )
    return Page[ReviewResponse](
        items=await project_reviews(db, reviews),
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/review-queue",
    response_model=Page[ReviewQueueItem],
    status_code=status.HTTP_200_OK,
    summary="List flags awaiting review",
    description=(
        "Paginated dashboard of flagged submissions whose completed reviews fall short of their scenario's "
        "`required_reviews`. Each item carries the submission summary, the required-review count, and the "
        "reviews so far. Filter by `evaluation_id`, `evaluation_group_id`, `scenario_id`, and/or "
        "`unassigned` (only flags that carry no live reviewer assignment — a flag whose single decided "
        "review leaves it short of `required_reviews` is still assigned)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
    },
)
async def review_queue_endpoint(
    caller: _ReadCaller,
    pagination: PaginationDep,
    filters: ReviewQueueFiltersDep,
    db: DbSession,
    settings: SettingsDep,
) -> Page[ReviewQueueItem]:
    """List flags awaiting review.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `reviews:read`.
    """
    entries, total = await review_queue(
        db,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        can_review=_can_review(caller),
        filters=filters,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    # Resolve every entry's reviewer emails in one query (not one per entry) — see resolve_reviewer_emails.
    emails = await resolve_reviewer_emails(db, [review for entry in entries for review in entry.reviews])
    return Page[ReviewQueueItem](
        items=[
            ReviewQueueItem(
                submission=MessageFlagResponse.from_model(entry.flag, settings=settings),
                required_reviews=entry.required_reviews,
                completed_reviews=entry.completed_reviews,
                reviews=[
                    ReviewResponse.from_model(review, reviewer_email=emails.get(review.reviewer_id))
                    for review in entry.reviews
                ],
            )
            for entry in entries
        ],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/reviews/{review_id}",
    response_model=ReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Get review",
    description="Fetch one review the caller may read.",
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _REVIEW_NOT_FOUND,
    },
)
async def get_review_endpoint(
    review_id: UUID,
    caller: _ReadCaller,
    db: DbSession,
) -> ReviewResponse:
    """Fetch one review.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `reviews:read`.
    * **404 Not Found** — no such review visible to the caller.
    """
    review = await get_review(
        db, review_id, caller_id=caller.id, can_manage=_can_manage(caller), can_review=_can_review(caller)
    )
    return await project_review(db, review)


@router.patch(
    "/reviews/{review_id}",
    response_model=ReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Record a verdict",
    description=(
        "Record the reviewer's verdict (`successful_exploit` / `unique_exploit` / `valid_submission` / "
        "`number_prompts` / `notes`) and set `status` to `approved` / `rejected`. Requires `reviews:update`, "
        "and only the assigned reviewer (or a break-glass manager) may record it."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _VERDICT_FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _REVIEW_NOT_FOUND,
    },
)
@transactional
async def update_review_endpoint(
    review_id: UUID,
    payload: ReviewUpdate,
    caller: _UpdateCaller,
    db: DbSession,
) -> ReviewResponse:
    """Record a reviewer's verdict.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `reviews:update`, or isn't the assigned reviewer (nor an admin).
    * **404 Not Found** — no such review visible to the caller.
    """
    can_manage = _can_manage(caller)
    review = await get_review(
        db, review_id, caller_id=caller.id, can_manage=can_manage, can_review=_can_review(caller), for_update=True
    )
    if not can_manage and review.reviewer_id != caller.id:
        raise ForbiddenError("Only the assigned reviewer can record this verdict.")
    before = _review_snapshot(review)
    changes = ReviewUpdateChanges.model_validate(payload.model_dump(exclude_unset=True))
    updated = await update_review(db, review, changes)
    diff_before, diff_after = changed_fields(before, _review_snapshot(updated))
    if diff_before or diff_after:
        await record_audit(
            db,
            actor_id=caller.id,
            actor_email=caller.email,
            action=AuditAction.REVIEW_VERDICT,
            object_type="review",
            object_id=updated.id,
            before=diff_before,
            after=diff_after,
        )
    return await project_review(db, updated)


@router.post(
    "/reviews/{review_id}/restore",
    response_model=ReviewResponse,
    status_code=status.HTTP_200_OK,
    summary="Re-assign an unassigned reviewer",
    description=(
        "Clear an unassigned review's `deleted_at`, putting the reviewer back on the submission with any "
        "verdict they had recorded. Restorable for a fixed window after the unassign (set per deployment), "
        "and only your own unassignment unless you hold the break-glass (`evaluation_groups:manage`), which "
        "restores any actor's; a decided review additionally only by the reviewer who recorded it or that "
        "break-glass. A `pending` review is restored only while it is still actionable — the submission "
        "undecided and the reviewer live and assignable, as on a fresh assignment. The reviewer is emailed "
        "as on a fresh assignment (a decided review's since-deleted reviewer is not)."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _RESTORE_FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _NOT_RESTORABLE,
        status.HTTP_409_CONFLICT: problem_response(
            "That reviewer is already assigned to this submission, or the submission was decided while the "
            "review was unassigned."
        ),
    },
)
@transactional
async def restore_review_endpoint(
    review_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
    settings: SettingsDep,
) -> ReviewResponse:
    """Restore an unassigned review (put the reviewer back on the submission).

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `reviews:delete`, or may not restore a decided
      review they didn't record.
    * **404 Not Found** — no restorable review: never unassigned, unassigned longer than
      the restore window ago, unassigned by someone else, not visible to the caller, or
      (for a pending review) its reviewer is no longer live or assignable.
    * **409 Conflict** — the reviewer has since been re-assigned to this submission, or
      the submission was decided while the review was unassigned.
    """
    can_manage = _can_manage(caller)
    review = await get_restorable_review(
        db,
        review_id,
        caller_id=caller.id,
        can_manage=can_manage,
        can_review=_can_review(caller),
        deleted_cutoff=restore_cutoff(settings),
    )
    # Unreachable under the deleter scope today (a non-manager only reaches their own
    # tombstone, and only its reviewer may unassign a decided review) — kept as defence
    # in depth should that scope ever widen.
    if review.status != ReviewStatus.PENDING and not can_manage and review.reviewer_id != caller.id:
        raise ForbiddenError("Only the reviewer who recorded a verdict (or a manager) can restore a decided review.")
    await assert_review_reassignable(db, review)
    restored = await restore_review(db, review)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.REVIEW_RESTORE,
        object_type="review",
        object_id=restored.id,
        after=_review_snapshot(restored),
    )
    await notify_review_assigned(db, restored, actor=caller)
    return await project_review(db, restored)


@router.delete(
    "/reviews/{review_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unassign a reviewer",
    description=(
        "Soft-delete a review (unassign the reviewer); subsequent reads exclude it. A `pending` review may be "
        "unassigned by any reviewer in the group; a review that already carries a verdict can only be removed by "
        "the reviewer who recorded it or a break-glass manager."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _UNASSIGN_FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _REVIEW_NOT_FOUND,
    },
)
@transactional
async def unassign_review_endpoint(
    review_id: UUID,
    caller: _DeleteCaller,
    db: DbSession,
) -> Response:
    """Unassign a reviewer (soft-delete the review).

    A still-`pending` review is roster management — anyone holding `reviews:delete`
    in the group may unassign it. A review that already carries a verdict is locked
    like the verdict write: only its own reviewer (or a break-glass manager) may
    remove it, so a peer can't erase someone else's recorded verdict.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `reviews:delete`, or may not unassign a decided review they didn't record.
    * **404 Not Found** — no such review visible to the caller.
    """
    can_manage = _can_manage(caller)
    review = await get_review(
        db, review_id, caller_id=caller.id, can_manage=can_manage, can_review=_can_review(caller), for_update=True
    )
    if review.status != ReviewStatus.PENDING and not can_manage and review.reviewer_id != caller.id:
        raise ForbiddenError("Only the reviewer who recorded a verdict (or a manager) can unassign a decided review.")
    before = _review_snapshot(review)
    await unassign_review(db, review, by_id=caller.id)
    await record_audit(
        db,
        actor_id=caller.id,
        actor_email=caller.email,
        action=AuditAction.REVIEW_UNASSIGN,
        object_type="review",
        object_id=review.id,
        before=before,
    )
    await notify_review_unassigned(db, review, actor=caller)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/submissions/{submission_id}",
    response_model=SubmissionDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Get submission detail",
    description=(
        "Fetch one flagged submission with its flagged messages and the reviews recorded so far — the "
        "reviewer's detail view. A reviewer (annotator / owner / admin) sees any submission in a visible group; "
        "a red-teamer only their own. The parent conversation is referenced via `submission.conversation_id`."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _SUBMISSION_NOT_FOUND,
    },
)
async def get_submission_detail_endpoint(
    submission_id: UUID,
    caller: _ReadCaller,
    pagination: PaginationDep,
    db: DbSession,
    settings: SettingsDep,
) -> SubmissionDetailResponse:
    """Fetch one submission's detail — body, flagged messages, and a page of reviews.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `reviews:read`.
    * **404 Not Found** — no such submission visible to the caller.
    """
    flag, superseded_ids, reviews, total = await get_submission_detail(
        db,
        submission_id,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        can_review=_can_review(caller),
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return SubmissionDetailResponse(
        submission=MessageFlagResponse.from_model(flag, settings=settings),
        reviews=Page[ReviewResponse](
            items=await project_reviews(db, reviews),
            total=total,
            limit=pagination.limit,
            offset=pagination.offset,
        ),
        superseded_message_ids=sorted(superseded_ids),
    )


@router.get(
    "/submissions/{submission_id}/messages",
    response_model=Page[TranscriptMessage],
    status_code=status.HTTP_200_OK,
    summary="Get submission parent-conversation messages",
    description=(
        "Paginated transcript of the submission's parent conversation (oldest first; superseded messages "
        "excluded) — the reviewer's full context. Review-scoped: a reviewer sees any submission in a visible "
        "group, a red-teamer only their own. The owner-scoped conversation history endpoint is unavailable to a "
        "non-owner reviewer, so the transcript is read here."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _SUBMISSION_NOT_FOUND,
    },
)
async def get_submission_messages_endpoint(
    submission_id: UUID,
    caller: _ReadCaller,
    pagination: PaginationDep,
    db: DbSession,
    settings: SettingsDep,
) -> Page[TranscriptMessage]:
    """List the parent conversation's messages for a submission the caller may review.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `reviews:read`.
    * **404 Not Found** — no such submission visible to the caller.
    """
    messages, total = await submission_conversation_messages(
        db,
        submission_id,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        can_review=_can_review(caller),
        limit=pagination.limit,
        offset=pagination.offset,
    )
    image_keys = await image_keys_for_messages(db, [message.id for message in messages])
    return Page[TranscriptMessage](
        items=[
            TranscriptMessage.from_model(message, settings=settings, image_keys=image_keys.get(message.id, ()))
            for message in messages
        ],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get(
    "/submissions/{submission_id}/assignable-reviewers",
    response_model=Page[AssignableReviewerResponse],
    status_code=status.HTTP_200_OK,
    summary="List assignable reviewers for a submission",
    description=(
        "Return the users assignable as a reviewer to this submission — the flag group's assignable-reviewer "
        "pool minus the flag's author and anyone already assigned. Exactly the set `POST /reviews` accepts, so "
        "the assign picker never offers a user the assign call would reject."
        " Each candidate carries `active_review_count`, the open review work they already hold, so the picker "
        "can show how loaded they are before you add one more."
    ),
    responses=COMMON_ERROR_RESPONSES
    | {
        status.HTTP_401_UNAUTHORIZED: _UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: _FORBIDDEN,
        status.HTTP_404_NOT_FOUND: _SUBMISSION_NOT_FOUND,
    },
)
async def list_assignable_reviewers_endpoint(
    submission_id: UUID,
    caller: _AssignCaller,
    pagination: PaginationDep,
    db: DbSession,
    search: Annotated[
        str | None,
        Query(description="Case-insensitive substring match on reviewer email / first / last name.", max_length=320),
    ] = None,
) -> Page[AssignableReviewerResponse]:
    """List the users assignable as a reviewer to one submission.

    ### Errors

    * **401 Unauthorized** — bearer token missing/invalid.
    * **403 Forbidden** — caller lacks `reviews:create`.
    * **404 Not Found** — no such submission visible to the caller.
    """
    users, total = await list_assignable_reviewers(
        db,
        submission_id,
        caller_id=caller.id,
        can_manage=_can_manage(caller),
        search=search,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    # One aggregate for the whole page, like the queue's reviewer-email resolve — not one per user.
    workload = await count_active_reviews(db, [user.id for user in users])
    return Page[AssignableReviewerResponse](
        items=[AssignableReviewerResponse.from_user_with_workload(user, workload.get(user.id, 0)) for user in users],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
    )
