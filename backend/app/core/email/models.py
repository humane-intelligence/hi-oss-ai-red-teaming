"""SQLModel table for the email audit / debug log.

One row per logical mail — tracks template, recipient, context, backend,
lifecycle status, retry counter, and error info. Lets ops trace
"why didn't user X get the mail" without re-running the task.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from app.core.base_model import BaseModel


class OutboundEmailStatus(StrEnum):
    """Lifecycle state of an `OutboundEmail` row.

    `queued` is set on insert by `send_email()`; the worker flips to `sent`
    on successful delivery or `failed` on a permanent error / exhausted retry
    budget. Intermediate transient retries leave the row on `queued`.
    """

    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"


class OutboundEmail(BaseModel, table=True):
    """Audit row for one logical mail (one row per send, not per attempt).

    `context` stores the validated template context; the worker reads it back
    here at render time, so callers must not put secrets (tokens, OTPs) into
    it — pass a handle and resolve in the task instead.
    """

    __tablename__ = "outbound_emails"

    template_name: str = Field(index=True, max_length=64)
    recipient: str = Field(index=True, max_length=320)
    context: dict[str, Any] = Field(sa_type=JSONB, sa_column_kwargs={"nullable": False})
    backend: str = Field(max_length=32)
    subject: str | None = Field(default=None, max_length=998)
    status: OutboundEmailStatus = Field(
        default=OutboundEmailStatus.QUEUED,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            OutboundEmailStatus,
            values_callable=lambda enum: [m.value for m in enum],
            name="outboundemailstatus",
        ),
        sa_column_kwargs={"nullable": False, "index": True},
    )
    # Who asked for this mail, when a human did. Nullable: system-triggered sends
    # (self-service re-issue, verification) have nobody to notify. Drives the
    # delivery-failure notification — the requester is the only party who can act
    # on "the invite never arrived".
    requested_by_user_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="users.id",
        nullable=True,
        index=True,
    )
    # Correlates the mails of one bulk request. Not a FK — it names no row, it groups
    # these ones. Its only job is to keep a batch's delivery failures from writing one
    # notification per row: the worker notifies for the first failure of a batch only.
    batch_key: uuid.UUID | None = Field(default=None, nullable=True, index=True)
    error_type: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=2000)
    attempts: int = Field(default=0)
    sent_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    celery_task_id: str | None = Field(default=None, index=True, max_length=64)
    # ESP-side message id for cross-referencing (e.g. SES MessageId, SendGrid X-Message-Id).
    # Stays NULL until a backend that returns one lands.
    provider_message_id: str | None = Field(default=None, index=True, max_length=255)
