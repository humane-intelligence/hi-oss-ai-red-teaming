"""Review-domain tables — `Review`, one reviewer's verdict on a `MessageFlag`.

A `Review` is a reviewer's assignment-plus-verdict on a flagged submission
(`MessageFlag`, the "submission"): creating one assigns a reviewer (`status`
lands `pending`), submitting the verdict moves it to `approved` / `rejected`. A
flag carries many reviews, at most one per reviewer (the partial-unique index),
so consensus over an odd `Scenario.required_reviews` is possible — that count is
how many completed verdicts the review queue waits for.

`evaluation_id` is denormalised from the parent flag at create time (under the
caller's visibility), so every read scopes through the shared
`join_visible_evaluation_group` spine with a single join, exactly like
`MessageFlag`. The verdict columns stay null until the reviewer submits them.
"""

import uuid

from sqlalchemy import Enum
from sqlalchemy import Index
from sqlalchemy import Text
from sqlalchemy import text
from sqlmodel import Field

from app.core.base_model import BaseModel
from app.core.reviews.enums import ReviewStatus


class Review(BaseModel, table=True):
    __tablename__ = "reviews"
    __table_args__ = (
        # At most one live review per reviewer per flag (the partial index survives soft-delete).
        Index(
            "ix_reviews_message_flag_id_reviewer_id",
            "message_flag_id",
            "reviewer_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # The review queue and per-evaluation listing scope by the denormalised evaluation.
        Index("ix_reviews_evaluation_id", "evaluation_id"),
    )

    message_flag_id: uuid.UUID = Field(foreign_key="message_flags.id", nullable=False, ondelete="CASCADE")
    reviewer_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    # Who assigned the reviewer (the caller of the assign endpoint) — audit attribution.
    assigned_by_id: uuid.UUID = Field(foreign_key="users.id", nullable=False)
    evaluation_id: uuid.UUID = Field(foreign_key="evaluations.id", nullable=False, ondelete="CASCADE")
    status: ReviewStatus = Field(
        default=ReviewStatus.PENDING,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            ReviewStatus,
            values_callable=lambda enum: [m.value for m in enum],
            name="reviewstatus",
        ),
        sa_column_kwargs={"nullable": False, "index": True, "server_default": text("'pending'")},
    )
    successful_exploit: bool | None = Field(default=None)
    unique_exploit: bool | None = Field(default=None)
    valid_submission: bool | None = Field(default=None)
    number_prompts: int | None = Field(default=None)
    notes: str | None = Field(default=None, sa_type=Text)
