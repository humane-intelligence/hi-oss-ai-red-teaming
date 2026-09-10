"""Request/response schemas for the message-flag and note endpoints.

`MessageFlagResponse` is the public projection; `MessageFlagCreate` /
`MessageFlagUpdate` back the write paths. `MessageFlagUpdateChanges` is the
service-owned, HTTP-agnostic update contract — building it from
`payload.model_dump(exclude_unset=True)` lets `model_fields_set` separate an
omitted field from an explicit `null` (the latter clears the nullable `comment`).
"""

from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from app.core.annotations.enums import FlagStatus
from app.core.config import Settings
from app.core.conversations.schemas import MessageBase
from app.core.helpers import sanitise_single_line

if TYPE_CHECKING:
    from app.core.annotations.models import Annotation
    from app.core.annotations.models import AnnotationLabel
    from app.core.annotations.models import MessageFlag
    from app.core.annotations.models import Note
    from app.core.annotations.models import TaskCompletion

_EXAMPLE_MESSAGE_FLAG_RESPONSE: dict[str, Any] = {
    "id": "c1d2e3f4-5a6b-4c7d-8e9f-0a1b2c3d4e5f",
    "messages": [
        {
            "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
            "role": "assistant",
            "status": "complete",
            "content": "Here is the step-by-step procedure you asked for…",
            "slot": "a",
            "created_at": "2026-01-01T12:00:00Z",
            "tag_context": {"env": "prod"},
            "tag_context_partial": False,
        }
    ],
    "reason": "Model produced step-by-step instructions for the restricted task.",
    "red_flagged": True,
    "comment": "Reproduced twice; see the final assistant turn.",
    "status": "pending",
    "created_by_id": "a1b2c3d4-1111-2222-3333-444455556666",
    "conversation_id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
    "evaluation_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    "evaluation_group_id": "2c8e5acd-aafd-4a1d-8a4d-9a7ceaac3adc",
    "scenario_id": "9a7b6c5d-1e2f-4a3b-8c9d-0e1f2a3b4c5d",
    "task_id": "5d4c3b2a-9f8e-4d3c-2b1a-0f9e8d7c6b5a",
    "created_at": "2026-01-01T12:00:00Z",
    "updated_at": "2026-01-02T09:30:00Z",
}


# Build through `from_model` so the projection stays in one place — kept out of the docstring, which
# Pydantic publishes as this schema's `description` in the OpenAPI contract.
class MessageFlagResponse(BaseModel):
    """Public view of a `MessageFlag` row."""

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_MESSAGE_FLAG_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier.")
    conversation_id: UUID = Field(description="Conversation the flagged messages belong to.")
    messages: list[MessageBase] = Field(
        description="The selected messages of the conversation (base data), chronologically ordered."
    )
    reason: str = Field(description="Why the selection is exploit-worthy.")
    red_flagged: bool = Field(description="The red-teamer's exploit-worthy assertion.")
    comment: str | None = Field(default=None, description="Optional free-text note from the author.")
    status: FlagStatus = Field(description="Review verdict; `pending` until the review workflow lands.")
    created_by_id: UUID = Field(description="User who authored the flag.")
    evaluation_id: UUID = Field(description="Evaluation the conversation runs against.")
    evaluation_group_id: UUID = Field(description="Top-level evaluation group.")
    scenario_id: UUID | None = Field(default=None, description="Scenario the conversation targets, if any.")
    task_id: UUID | None = Field(default=None, description="Task of the scenario the flag pins, if any.")
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who soft-deleted the flag; null on live rows and on tombstones predating the field.",
    )

    @classmethod
    def from_model(cls, flag: MessageFlag, *, settings: Settings) -> MessageFlagResponse:
        """Project a `MessageFlag` ORM row into the public response shape.

        `settings` reaches the embedded messages: a flag carries the text it was raised on, so a
        sealed transcript has to open here too or the annotator handling the flag reads ciphertext.
        """
        return cls(
            id=flag.id,
            conversation_id=flag.conversation_id,
            messages=[MessageBase.from_model(message, settings=settings) for message in flag.messages],
            reason=flag.reason,
            red_flagged=flag.red_flagged,
            comment=flag.comment,
            status=flag.status,
            created_by_id=flag.created_by_id,
            evaluation_id=flag.evaluation_id,
            evaluation_group_id=flag.evaluation_group_id,
            scenario_id=flag.scenario_id,
            task_id=flag.task_id,
            created_at=flag.created_at,
            updated_at=flag.updated_at,
            deleted_at=flag.deleted_at,
            deleted_by_id=flag.deleted_by_id,
        )


class MessageFlagCreate(BaseModel):
    """Payload accepted by `POST /v1/message-flags`.

    The owner is the authenticated caller; the ancestry beyond the conversation
    (evaluation/group/scenario) is resolved from `conversation_id` — not in the
    payload. `message_ids` is the selected set (one, a range, or the whole
    conversation); every id must be a message of `conversation_id`. `task_id`,
    when given, must be a task of the conversation's scenario.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "conversation_id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
                "message_ids": [
                    "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                    "8d0f7780-8536-41ef-a55c-f18cd2e01bf8",
                ],
                "reason": "Model produced step-by-step instructions for the restricted task.",
                "red_flagged": True,
                "comment": "Reproduced twice; see the final assistant turn.",
                "task_id": "5d4c3b2a-9f8e-4d3c-2b1a-0f9e8d7c6b5a",
            }
        }
    )

    conversation_id: UUID = Field(description="Conversation whose messages are being flagged.")
    message_ids: list[UUID] = Field(
        min_length=1, description="The conversation messages to flag — at least one; duplicates are rejected."
    )
    reason: str = Field(min_length=1, description="Why the selection is exploit-worthy.")
    red_flagged: bool = Field(default=True, description="The red-teamer's exploit-worthy assertion.")
    comment: str | None = Field(default=None, description="Optional free-text note from the author.")
    task_id: UUID | None = Field(
        default=None, description="Task of the conversation's scenario to pin the flag to, if any."
    )

    @field_validator("message_ids")
    @classmethod
    def _reject_duplicates(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("must not contain duplicate message ids")
        return value


class MessageFlagUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/message-flags/{id}`.

    Content-only: `reason` / `red_flagged` / `comment`. The anchor (`message_ids`),
    ancestry, and `status` are not editable here. Omitted fields stay unchanged;
    explicit `null` is rejected for `reason` / `red_flagged` (they back NOT NULL
    columns), but allowed for `comment` (clears the optional note).
    """

    model_config = ConfigDict(
        json_schema_extra={"example": {"reason": "Revised: only reproduces with a jailbreak preamble."}}
    )

    reason: str | None = Field(default=None, min_length=1, description="Omit to leave unchanged.")
    red_flagged: bool | None = Field(default=None, description="Omit to leave unchanged.")
    comment: str | None = Field(default=None, description="Omit to leave unchanged; send `null` to clear.")

    @field_validator("reason", "red_flagged", mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # Both back NOT NULL columns; omitting leaves the row untouched, but an
        # explicit `null` would crash the flush. Reject at the edge with a 422.
        # `comment` is intentionally excluded — `null` there clears the note.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


class MessageFlagUpdateChanges(BaseModel):
    """Service-owned, HTTP-agnostic update contract.

    `extra="forbid"` makes drift from `MessageFlagUpdate` fail loudly at
    construction. Build from `payload.model_dump(exclude_unset=True)` so
    `model_fields_set` separates "omitted" from "explicit None" (the latter only
    valid for `comment`).
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
    red_flagged: bool | None = None
    comment: str | None = None


_NOTE_TEXT_MAX_LENGTH = 10_000
NOTE_MAX_MESSAGE_IDS = 500

_EXAMPLE_NOTE_RESPONSE: dict[str, Any] = {
    "id": "d4e5f6a7-8b9c-4d0e-1f2a-3b4c5d6e7f8a",
    "conversation_id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
    "message_ids": [
        "7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "8d0f7780-8536-41ef-a55c-f18cd2e01bf8",
    ],
    "text": "Refuses on the first ask, then complies once the request is reframed as fiction.",
    "created_by_id": "a1b2c3d4-1111-2222-3333-444455556666",
    "evaluation_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    "evaluation_group_id": "2c8e5acd-aafd-4a1d-8a4d-9a7ceaac3adc",
    "created_at": "2026-01-01T12:00:00Z",
    "updated_at": "2026-01-02T09:30:00Z",
}


class NoteResponse(BaseModel):
    """Public view of a live `Note`, referencing its selection by id.

    The noted messages are not embedded: a client renders notes against a
    transcript it has already fetched, so their ids are enough.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_NOTE_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier.")
    conversation_id: UUID = Field(description="Conversation the noted messages belong to.")
    message_ids: list[UUID] = Field(
        description="Ids of the noted messages, oldest first — the selection made at create time."
    )
    text: str = Field(description="The annotator's note on the selection.")
    created_by_id: UUID = Field(description="User who authored the note.")
    evaluation_id: UUID = Field(description="Evaluation the conversation runs against.")
    evaluation_group_id: UUID = Field(description="Top-level evaluation group.")
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who soft-deleted the note; null on live rows and on tombstones predating the field.",
    )

    @classmethod
    def from_model(cls, note: Note) -> NoteResponse:
        """Project a `Note` ORM row into the public response shape."""
        return cls(
            id=note.id,
            conversation_id=note.conversation_id,
            # From the ordered relationship, not the link rows — the order is the
            # transcript's own (`created_at, role, slot, id`), so a turn's prompt
            # precedes its reply even when both were written in one transaction.
            message_ids=[message.id for message in note.messages],
            text=note.text,
            created_by_id=note.created_by_id,
            evaluation_id=note.evaluation_id,
            evaluation_group_id=note.evaluation_group_id,
            created_at=note.created_at,
            updated_at=note.updated_at,
            deleted_at=note.deleted_at,
            deleted_by_id=note.deleted_by_id,
        )


class NoteCreate(BaseModel):
    """Payload accepted by `POST /v1/notes`.

    The author is the authenticated caller; the ancestry beyond the conversation
    (evaluation/group) is resolved from `conversation_id`, not the payload.
    `message_ids` is the selected set and is fixed once written — a different
    selection means a new note.

    Every id must be a live message of the conversation; unmatched ids are listed in
    the 404, which the cap on `message_ids` bounds.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "conversation_id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
                "message_ids": [
                    "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                    "8d0f7780-8536-41ef-a55c-f18cd2e01bf8",
                ],
                "text": "Refuses on the first ask, then complies once the request is reframed as fiction.",
            }
        }
    )

    conversation_id: UUID = Field(description="Conversation whose messages are being noted.")
    message_ids: list[UUID] = Field(
        min_length=1,
        max_length=NOTE_MAX_MESSAGE_IDS,
        description=(
            f"The conversation messages to note — at least one, at most {NOTE_MAX_MESSAGE_IDS}; "
            "duplicates are rejected."
        ),
    )
    text: str = Field(
        min_length=1, max_length=_NOTE_TEXT_MAX_LENGTH, description="The annotator's note on the selection."
    )

    @field_validator("message_ids")
    @classmethod
    def _reject_duplicates(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("must not contain duplicate message ids")
        return value


class NoteUpdate(BaseModel):
    """Payload accepted by `PATCH /v1/notes/{id}`.

    Content-only: `text`. The conversation anchor, the selected message set and the
    ancestry are not editable — a different selection is a new note. Omitting
    the field leaves the row unchanged; an explicit `null` is rejected (it backs a
    NOT NULL column). An unknown field — `message_ids` above all — is rejected too,
    rather than accepted and ignored.
    """

    # `extra="forbid"` where `MessageFlagUpdate` allows extras: the message set is
    # advertised as fixed, so sending it must fail loudly rather than be dropped.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"text": "Revised: only reproduces with the fiction framing."}},
    )

    text: str | None = Field(
        default=None,
        min_length=1,
        max_length=_NOTE_TEXT_MAX_LENGTH,
        description="Omit to leave unchanged.",
    )

    @field_validator("text", mode="before")
    @classmethod
    def _reject_explicit_null(cls, value: Any) -> Any:
        # Backs a NOT NULL column; omitting leaves the row untouched, but an explicit
        # `null` would crash the flush. Reject at the edge with a 422.
        if value is None:
            raise ValueError("must not be null; omit the field to leave it unchanged")
        return value


class NoteUpdateChanges(BaseModel):
    """Service-owned, HTTP-agnostic update contract.

    `extra="forbid"` makes a field `NoteUpdate` gained and this model did not
    fail loudly at construction; the reverse drift is silent (a field only this
    model carries is never in `model_fields_set`, so `update_note` never
    writes it). Build from `payload.model_dump(exclude_unset=True)` so
    `model_fields_set` separates "omitted" from "explicitly sent".
    """

    model_config = ConfigDict(extra="forbid")

    text: str | None = None


_EXAMPLE_TASK_COMPLETION_RESPONSE: dict[str, Any] = {
    "id": "b2c3d4e5-6f7a-4b8c-9d0e-1f2a3b4c5d6e",
    "conversation_id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
    "task_id": "5d4c3b2a-9f8e-4d3c-2b1a-0f9e8d7c6b5a",
    "conversation_group_id": "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    "evaluation_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    "evaluation_group_id": "2c8e5acd-aafd-4a1d-8a4d-9a7ceaac3adc",
    "scenario_id": "9a7b6c5d-1e2f-4a3b-8c9d-0e1f2a3b4c5d",
    "created_by_id": "a1b2c3d4-1111-2222-3333-444455556666",
    "completed_at": "2026-01-01T12:00:00Z",
}


class TaskCompletionResponse(BaseModel):
    """Public view of a live `TaskCompletion` row.

    `completed_at` is the row's `created_at` (a live row *is* the completion), so
    the field name reads at the API boundary as "when the task was checked off".
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_TASK_COMPLETION_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier.")
    conversation_id: UUID = Field(description="Conversation the task was checked off in.")
    task_id: UUID = Field(description="The completed task.")
    conversation_group_id: UUID = Field(
        description=(
            "Group the conversation belonged to when the task was checked off — a create-time "
            "snapshot, not re-resolved if the conversation is later moved between groups."
        )
    )
    evaluation_id: UUID = Field(description="Evaluation the conversation runs against.")
    evaluation_group_id: UUID = Field(description="Top-level evaluation group.")
    scenario_id: UUID | None = Field(default=None, description="Scenario the task belongs to, if still live.")
    created_by_id: UUID = Field(description="User who checked the task off.")
    completed_at: datetime = Field(description="UTC timestamp the task was checked off.")

    @classmethod
    def from_model(cls, completion: TaskCompletion) -> TaskCompletionResponse:
        """Project a `TaskCompletion` ORM row into the public response shape."""
        return cls(
            id=completion.id,
            conversation_id=completion.conversation_id,
            task_id=completion.task_id,
            conversation_group_id=completion.conversation_group_id,
            evaluation_id=completion.evaluation_id,
            evaluation_group_id=completion.evaluation_group_id,
            scenario_id=completion.scenario_id,
            created_by_id=completion.created_by_id,
            completed_at=completion.created_at,
        )


class TaskCompletionCount(BaseModel):
    """How many of a group's conversations have a given task checked off."""

    task_id: UUID = Field(description="The task.")
    completed_count: int = Field(description="Conversations of the group with this task checked off (live).")


class GroupTaskCompletionRollup(BaseModel):
    """Read-only per-task roll-up across one conversation group.

    `total_conversations` is the group's live conversation count (the "N" in
    "K/N"); `completed_counts` carries only tasks with at least one completion —
    the client defaults every other task to 0 (mirroring `flag_counts_by_message`).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_conversations": 2,
                "completed_counts": [{"task_id": "5d4c3b2a-9f8e-4d3c-2b1a-0f9e8d7c6b5a", "completed_count": 1}],
            }
        }
    )

    total_conversations: int = Field(description="Live conversations in the group (the denominator).")
    completed_counts: list[TaskCompletionCount] = Field(
        description="Per-task completed-conversation counts; tasks with none are omitted."
    )


_EXAMPLE_ANNOTATION_LABEL_RESPONSE: dict[str, Any] = {
    "id": "5f1c7a90-8d3b-4e2a-9c11-6b7d0e4f2a83",
    "key": "jailbreak",
    "name": "Jailbreak",
    "is_custom": False,
}


class AnnotationLabelResponse(BaseModel):
    """One label an annotation can carry — curated, or one an annotator typed."""

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_ANNOTATION_LABEL_RESPONSE})

    id: UUID = Field(description="Stable identifier an annotation references.")
    key: str | None = Field(
        default=None,
        description="Stable catalog key — unchanged when the display name is reworded; null for a label an "
        "annotator named rather than one from the curated vocabulary.",
    )
    name: str = Field(description="Display wording, for the label picker.")
    is_custom: bool = Field(
        description="True for a label scoped to one annotator — theirs, plus visible in the conversations it is "
        "used in; false for the shared curated vocabulary. It marks *user-scoped*, not *typed*: naming a "
        "curated wording resolves to the curated row, which comes back false."
    )

    @classmethod
    def from_model(cls, label: AnnotationLabel) -> AnnotationLabelResponse:
        """Project an `AnnotationLabel` ORM row into the public response shape."""
        return cls(id=label.id, key=label.key, name=label.name, is_custom=label.created_by_id is not None)


# The catalog display-name cap: an ad-hoc label plays the same role on screen.
ANNOTATION_TEXT_MAX_LENGTH = 128

_EXAMPLE_ANNOTATION_RESPONSE: dict[str, Any] = {
    "id": "0b8e2f5d-6a1c-4d3e-9f70-2c4b8a6d1e35",
    "message_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "conversation_id": "f3a1d2b4-1c2e-4f56-8a7b-9c0d1e2f3a4b",
    "label": _EXAMPLE_ANNOTATION_LABEL_RESPONSE,
    "created_by_id": "b7f4c1d0-2e3a-4b5c-8d9e-0f1a2b3c4d5e",
    "evaluation_id": "d2c3b4a5-6f70-4812-9a3b-4c5d6e7f8091",
    "evaluation_group_id": "e5f60718-293a-4b4c-8d5e-6f7081920a3b",
    "created_at": "2026-08-27T12:00:00Z",
    "updated_at": "2026-08-27T12:00:00Z",
}


class AnnotationCreate(BaseModel):
    """Payload accepted by `POST /v1/annotations`.

    Exactly one of `label_id` (an existing label) and `text` (a label named by hand) is set.
    A `text` resolves to the curated row of that wording if one exists, else the caller's own,
    else a new one scoped to them. The author is the
    authenticated caller; the ancestry is resolved from `message_id`, not the payload. There
    is no update: the row is fixed at create, and a different label is a new annotation.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
                "label_id": "5f1c7a90-8d3b-4e2a-9c11-6b7d0e4f2a83",
            }
        }
    )

    message_id: UUID = Field(description="The message being labelled.")
    label_id: UUID | None = Field(
        default=None, description="An existing label, curated or the caller's own — mutually exclusive with `text`."
    )
    text: str | None = Field(
        default=None,
        min_length=1,
        max_length=ANNOTATION_TEXT_MAX_LENGTH,
        description="A label named by hand — mutually exclusive with `label_id`. Resolves to the curated row of "
        "that wording if one exists, else the caller's own, else a new one scoped to them.",
    )

    @field_validator("text")
    @classmethod
    def _sanitise(cls, value: str | None) -> str | None:
        """One visible line, like every label rendered as a chip; empty after folding is absent."""
        if value is None:
            return None
        cleaned = sanitise_single_line(value)
        return cleaned or None

    @model_validator(mode="after")
    def _exactly_one_label(self) -> AnnotationCreate:
        # The storage no longer enforces this (a typed label resolves to a row before it is
        # written), so the payload rule lives here alone — and reads as a 422, not a 500.
        if (self.label_id is None) == (self.text is None):
            raise ValueError("Provide exactly one of 'label_id' and 'text'.")
        return self


class AnnotationResponse(BaseModel):
    """Public view of an `Annotation`, with its label embedded.

    Embedding is load-bearing, not convenience: a retired label, and another annotator's
    custom one, are both absent from this caller's `GET /annotation-labels` — so `label_id`
    cannot always be resolved against that list, and the row has to carry its own display
    data.
    """

    model_config = ConfigDict(json_schema_extra={"example": _EXAMPLE_ANNOTATION_RESPONSE})

    id: UUID = Field(description="Server-assigned identifier.")
    message_id: UUID = Field(description="The labelled message.")
    conversation_id: UUID = Field(description="Conversation the message belongs to.")
    label: AnnotationLabelResponse = Field(description="The label carried, embedded — curated or an author's own.")
    created_by_id: UUID = Field(description="User who authored the annotation.")
    evaluation_id: UUID = Field(description="Evaluation the conversation runs against.")
    evaluation_group_id: UUID = Field(description="Top-level evaluation group.")
    created_at: datetime = Field(description="UTC timestamp of creation.")
    updated_at: datetime = Field(description="UTC timestamp of the last update.")
    deleted_at: datetime | None = Field(
        default=None,
        description="UTC timestamp of the soft-delete; null unless this row is a tombstone (`deleted=true` listings).",
    )
    deleted_by_id: UUID | None = Field(
        default=None,
        description="User who soft-deleted the annotation; null unless this row is a tombstone.",
    )

    @classmethod
    def from_model(cls, annotation: Annotation) -> AnnotationResponse:
        """Project an `Annotation` ORM row (with its label eager-loaded) into the public shape."""
        return cls(
            id=annotation.id,
            message_id=annotation.message_id,
            conversation_id=annotation.conversation_id,
            label=AnnotationLabelResponse.from_model(annotation.label),
            created_by_id=annotation.created_by_id,
            evaluation_id=annotation.evaluation_id,
            evaluation_group_id=annotation.evaluation_group_id,
            created_at=annotation.created_at,
            updated_at=annotation.updated_at,
            deleted_at=annotation.deleted_at,
            deleted_by_id=annotation.deleted_by_id,
        )
