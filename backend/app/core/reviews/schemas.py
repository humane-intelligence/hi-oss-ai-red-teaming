"""Request/response schemas for the review endpoints.

`ReviewResponse` is the public projection of a verdict row; `ReviewCreate`
assigns a reviewer (status lands `pending`); `ReviewUpdate` records the verdict
and flips `status` to `approved` / `rejected`. `ReviewUpdateChanges` is the
service-owned, HTTP-agnostic update contract — building it from
`payload.model_dump(exclude_unset=True)` lets `model_fields_set` separate an
omitted field from an explicit value. `ReviewQueueItem` is one entry of the
awaiting-review dashboard: the flagged submission plus its assigned reviews and
the required-review count.
"""

from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from app.core.annotations.schemas import _EXAMPLE_MESSAGE_FLAG_RESPONSE
from app.core.annotations.schemas import MessageFlagResponse
from app.core.bulk import BulkRequest
from app.core.evaluations.schemas import AnnotatorResponse
from app.core.reviews.enums import ReviewStatus
from app.core.schemas import Page

if TYPE_CHECKING:
    from app.core.auth.models import User
    from app.core.reviews.models import Review

_EXAMPLE_REVIEW_RESPONSE: dict[str, Any] = {
    "id": "d4e5f6a7-8b9c-4d0e-1f2a-3b4c5d6e7f80",
    "message_flag_id": "c1d2e3f4-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
    "reviewer_id": "a1b2c3d4-1111-2222-3333-444455556666",
    "assigned_by_id": "b2c3d4e5-2222-3333-4444-555566667777",
    "evaluation_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    "status": "approved",
    "successful_exploit": True,
    "unique_exploit": False,
    "valid_submission": True,
    "number_prompts": 4,
    "notes": "Reproduced; clear policy violation in the final turn.",
    "created_at": "2026-01-01T12:00:00Z",
    "updated_at": "2026-01-02T09:30:00Z",
}


class ReviewResponse(BaseModel):
    """Public view of a `Review` row.

    Build via `ReviewResponse.from_model(review)` so the projection stays in one
    place. Verdict fields are `null` until the reviewer submits them.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_REVIEW_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier.")
    message_flag_id: UUID = Field(description="Flagged submission under review.")
    reviewer_id: UUID = Field(description="User assigned to review the submission.")
    reviewer_email: str | None = Field(
        default=None,
        description="Email of the assigned reviewer. Resolved on the single-review projections "
        "(assign / get / update) and the read projections (list / queue / submission detail); "
        "`null` where not resolved (e.g. a bulk-assign row result).",
    )
    assigned_by_id: UUID = Field(description="User who assigned the reviewer.")
    evaluation_id: UUID = Field(description="Evaluation the submission belongs to.")
    status: ReviewStatus = Field(description="`pending` until a verdict is recorded, then `approved` / `rejected`.")
    successful_exploit: bool | None = Field(default=None, description="Whether the attempt was a successful exploit.")
    unique_exploit: bool | None = Field(default=None, description="Whether the exploit is unique.")
    valid_submission: bool | None = Field(default=None, description="Whether the submission is valid.")
    number_prompts: int | None = Field(default=None, description="Number of prompts the attempt used.")
    notes: str | None = Field(default=None, description="Free-text reviewer notes.")
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")
    deleted_at: datetime | None = Field(
        default=None,
        description=("UTC timestamp of the unassign; null unless this row is a tombstone (`deleted=true` listings)."),
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who unassigned the reviewer; null on a live row, or when the unassign predates this field.",
    )

    @classmethod
    def from_model(cls, review: Review, *, reviewer_email: str | None = None) -> ReviewResponse:
        """Project a `Review` ORM row into the public response shape.

        `reviewer_email` is resolved by the caller (batch-looked-up in read projections); left `None`
        where the reviewer identity isn't resolved, e.g. a bulk-assign per-row result.
        """
        return cls(
            id=review.id,
            message_flag_id=review.message_flag_id,
            reviewer_id=review.reviewer_id,
            reviewer_email=reviewer_email,
            assigned_by_id=review.assigned_by_id,
            evaluation_id=review.evaluation_id,
            status=review.status,
            successful_exploit=review.successful_exploit,
            unique_exploit=review.unique_exploit,
            valid_submission=review.valid_submission,
            number_prompts=review.number_prompts,
            notes=review.notes,
            created_at=review.created_at,
            updated_at=review.updated_at,
            deleted_at=review.deleted_at,
            deleted_by_id=review.deleted_by_id,
        )


class ReviewCreate(BaseModel):
    """Payload accepted by `POST /v1/reviews` — assign a reviewer to a flag.

    Creates a `pending` review for `reviewer_id` on `message_flag_id`, subject to
    the one-review-per-reviewer rule and the no-self-review guard.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message_flag_id": "c1d2e3f4-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
                "reviewer_id": "a1b2c3d4-1111-2222-3333-444455556666",
            }
        }
    )

    message_flag_id: UUID = Field(description="Flagged submission to assign a reviewer to.")
    reviewer_id: UUID = Field(description="User to assign as reviewer of the submission.")


class ReviewBulkRequest(BulkRequest[ReviewCreate]):
    """Bulk reviewer assignment — one row assigns one reviewer to one flag.

    Lets the client submit the whole flags-by-reviewers cartesian in one call; per-row
    failures (already-assigned, self-author, non-pending, non-assignable) are reported
    individually via the shared `BulkResponse`.
    """


class ReviewUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/reviews/{id}` — record a verdict.

    All fields optional; omitted fields stay unchanged. `status` may only be set
    to `approved` / `rejected` (the verdict) — `pending` and explicit `null` are
    rejected. The boolean/`notes` fields accept `null` to clear a previously set
    value.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "approved",
                "successful_exploit": True,
                "unique_exploit": False,
                "valid_submission": True,
                "number_prompts": 4,
                "notes": "Reproduced; clear policy violation in the final turn.",
            }
        }
    )

    status: ReviewStatus | None = Field(
        default=None, description="Verdict: `approved` or `rejected`. Omit to leave unchanged."
    )
    successful_exploit: bool | None = Field(default=None, description="Whether the attempt was a successful exploit.")
    unique_exploit: bool | None = Field(default=None, description="Whether the exploit is unique.")
    valid_submission: bool | None = Field(default=None, description="Whether the submission is valid.")
    number_prompts: int | None = Field(default=None, ge=0, description="Number of prompts the attempt used.")
    notes: str | None = Field(default=None, description="Free-text reviewer notes; send `null` to clear.")

    @field_validator("status", mode="before")
    @classmethod
    def _reject_null_and_pending(cls, value: Any) -> Any:
        # `status` backs a NOT NULL column and a verdict can't revert to pending:
        # reject both at the edge with a 422. Omit the field to leave it unchanged.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        if value in (ReviewStatus.PENDING, ReviewStatus.PENDING.value):
            raise ValueError("verdict status must be 'approved' or 'rejected'")
        return value


class ReviewUpdateChanges(BaseModel):
    """Service-owned, HTTP-agnostic update contract.

    `extra="forbid"` makes drift from `ReviewUpdate` fail loudly at construction.
    Build from `payload.model_dump(exclude_unset=True)` so `model_fields_set`
    separates "omitted" from an explicit value.
    """

    model_config = ConfigDict(extra="forbid")

    status: ReviewStatus | None = None
    successful_exploit: bool | None = None
    unique_exploit: bool | None = None
    valid_submission: bool | None = None
    number_prompts: int | None = None
    notes: str | None = None


class ReviewQueueItem(BaseModel):
    """One entry of the awaiting-review dashboard.

    A flagged submission whose completed reviews are fewer than its scenario's
    `required_reviews`, with its assigned reviews so far. `required_reviews`
    falls back to 1 when the flag targets no (live) scenario.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "submission": _EXAMPLE_MESSAGE_FLAG_RESPONSE,
                "required_reviews": 3,
                "completed_reviews": 1,
                "reviews": [_EXAMPLE_REVIEW_RESPONSE],
            }
        }
    )

    submission: MessageFlagResponse = Field(description="The flagged submission summary.")
    required_reviews: int = Field(description="How many completed reviews the submission needs.")
    completed_reviews: int = Field(description="Reviews already carrying a verdict (`approved` / `rejected`).")
    reviews: list[ReviewResponse] = Field(description="Reviews assigned to the submission so far.")


class SubmissionDetailResponse(BaseModel):
    """Full record of one flagged submission for a reviewer.

    The submission body plus its flagged messages, and the reviews (verdicts)
    recorded so far. The parent conversation is referenced via
    `submission.conversation_id`; the full transcript is a separate fetch, not
    inlined here.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "submission": _EXAMPLE_MESSAGE_FLAG_RESPONSE,
                "reviews": {"items": [_EXAMPLE_REVIEW_RESPONSE], "total": 1, "limit": 50, "offset": 0},
                "superseded_message_ids": [],
            }
        }
    )

    submission: MessageFlagResponse = Field(description="The flagged submission: body plus the flagged messages.")
    reviews: Page[ReviewResponse] = Field(description="Reviews recorded on the submission so far (paginated).")
    superseded_message_ids: list[UUID] = Field(
        default_factory=list,
        description=(
            "Flagged messages a later regenerate/continue superseded: still in `submission.messages` for "
            "provenance, but absent from the live transcript (`GET /submissions/{id}/messages`)."
        ),
    )


class AssignableReviewerResponse(AnnotatorResponse):
    """An assignable reviewer, with how much open review work they already carry."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "a1b2c3d4-1111-2222-3333-444455556666",
                "email": "ada@example.com",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "status": "active",
                "active_review_count": 3,
            }
        }
    )

    active_review_count: int = Field(
        ge=0,
        description=(
            "Pending reviews this user holds whose whole parent chain is live and whose evaluation is "
            "neither `completed` nor `rejected` and whose group is neither `inactive` nor `not_approved` "
            "— not their lifetime review total, and narrower than the "
            "review queue, which also lists work inside finished evaluations. Counted platform-wide, "
            "including groups the caller cannot see: it answers how loaded the person is, not how loaded "
            "they are on work you can account for."
        ),
    )

    @classmethod
    def from_user_with_workload(cls, user: User, active_review_count: int) -> AssignableReviewerResponse:
        """Project a `User` and their open-review count into the assignable-reviewer shape."""
        return cls(
            id=user.id,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            status=user.status,
            active_review_count=active_review_count,
        )
