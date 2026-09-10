"""Conversation table — a red-teamer's conversation against one model in an evaluation.

A `Conversation` is created empty (its `Message` history is a later concern) and
binds four things: the `user` who owns it, the parent `evaluation`, the
chosen model *within* that evaluation — the `EvaluationAiModel` assignment, not
the bare `AiModel`, so the conversation inherits the assignment's masking and its
layer of the inference-params cascade — and the `scenario` (the challenge of the
evaluation the conversation targets, always exactly one).

Every conversation belongs to a `ConversationGroup` (`conversation_group_id` is required):
a group is the grouping bucket a conversation lives in — created with one or
more conversations, gaining/losing members over its life. A conversation can be
moved to another group of the same evaluation; a group with no live
conversations left is itself removed (see `services/groups.py`).

`parameters` (from `InferenceParamsMixin`) is the most-specific layer of the
cascade: effective params are
`merge_inference_params(ai_model.parameters, assignment.parameters, conversation.parameters)`.

`evaluation_id` is kept explicit even though it is derivable from the assignment
(and the scenario): it keeps the owner/visibility list queries single-join and
matches the legacy shape. The create service validates that the assignment and
the scenario actually belong to this evaluation, so the denormalisation can't
drift.
"""

import uuid
from typing import Any

from sqlalchemy import Enum
from sqlalchemy import Index
from sqlalchemy import Text
from sqlalchemy import case
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field
from sqlmodel import Relationship
from sqlmodel import SQLModel
from sqlmodel import col

from app.core.ai_gateway.inference_params import InferenceParamsMixin
from app.core.base_model import BaseModel
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus


class ConversationGroup(BaseModel, table=True):
    """A named bucket grouping a red-teamer's conversations within one evaluation.

    Every `Conversation` belongs to exactly one group (the `conversation_group_id` link is
    required). A group is created with one or more conversations and gains or
    loses members over its life — a comparison of several models on one prompt is
    the motivating case (each model its own conversation, grouped here), but a
    group is just the grouping bucket, not a comparison-specific construct.

    The group owns nothing the conversations don't carry themselves — no
    inference params (each conversation keeps its own cascade layer), only the
    grouping `name` and the denormalised `evaluation_id` / `scenario_id`. It is
    soft-deleted when its last live conversation leaves (delete or move) and
    cascade-soft-deletes its members when itself deleted, so a live conversation
    never dangles under a dead group.
    """

    __tablename__ = "conversation_groups"

    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    evaluation_id: uuid.UUID = Field(
        foreign_key="evaluations.id",
        nullable=False,
        ondelete="CASCADE",
        index=True,
    )
    # The challenge shared by every member conversation. `CASCADE` governs a hard
    # delete only — scenarios soft-delete, so this normally outlives a tombstoned one.
    scenario_id: uuid.UUID = Field(
        foreign_key="scenarios.id",
        nullable=False,
        ondelete="CASCADE",
        index=True,
    )
    name: str = Field(max_length=255, nullable=False)

    # Member conversations. Loads do NOT filter soft-deleted rows — attach
    # `with_live(Conversation)` when eager-loading. The group's soft-delete
    # cascade to members is handled in the service layer; the FK `ON DELETE
    # CASCADE` only governs a hard delete.
    conversations: list[Conversation] = Relationship(
        back_populates="group",
        # `id` breaks the tie: `created_at` defaults to Postgres `now()`, which is
        # transaction-scoped, so members created in one request share it exactly and
        # the order would otherwise be unspecified. Arbitrary but stable — which is
        # what the console needs to number same-named members consistently.
        sa_relationship_kwargs={"order_by": "Conversation.created_at, Conversation.id"},
    )


class Conversation(BaseModel, InferenceParamsMixin, table=True):
    __tablename__ = "conversations"

    user_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
    evaluation_id: uuid.UUID = Field(
        foreign_key="evaluations.id",
        nullable=False,
        ondelete="CASCADE",
        index=True,
    )
    evaluation_ai_model_id: uuid.UUID = Field(
        foreign_key="evaluation_ai_models.id",
        nullable=False,
        index=True,
    )
    # The grouping bucket this conversation lives in — required. A conversation
    # may be moved to another group of the same evaluation. `ON DELETE CASCADE`
    # governs a hard delete; the soft-delete cascade is in the service layer.
    conversation_group_id: uuid.UUID = Field(
        foreign_key="conversation_groups.id",
        nullable=False,
        ondelete="CASCADE",
        index=True,
    )
    group: ConversationGroup = Relationship(back_populates="conversations")
    # The scenario of the evaluation this conversation targets. Scenarios are
    # soft-deleted, so removing one leaves this link pointing at a tombstoned
    # scenario — intentional: it records which challenge the attempt targeted, and
    # the conversation stays valid under its evaluation (the read path never
    # resolves the scenario). `ON DELETE CASCADE` only governs a hard delete,
    # which in practice arrives via the evaluation's own cascade anyway.
    scenario_id: uuid.UUID = Field(
        foreign_key="scenarios.id",
        nullable=False,
        ondelete="CASCADE",
        index=True,
    )
    title: str | None = Field(default=None, max_length=255)
    # Free-form key/value tags folded into the model's `system_prompt` at dispatch
    # (see app/core/conversations/tags.py). NOT NULL, defaults to no tags.
    tags: dict[str, str] = Field(
        default_factory=dict,
        sa_type=JSONB,
        sa_column_kwargs={"nullable": False, "server_default": text("'{}'::jsonb")},
    )
    # Resolved from the evaluation's effective licence when the conversation is created, and never
    # re-derived: a licence changed afterwards does not reach conversations that already exist.
    # Message writes read this instead of walking the group → evaluation → licence cascade on the
    # streaming path.
    content_protected: bool = Field(
        default=False,
        sa_column_kwargs={"nullable": False, "server_default": text("false")},
    )
    # `parameters` JSONB is inherited from `InferenceParamsMixin` — the
    # most-specific (conversation) override layer of the cascade.


class Turn(BaseModel, table=True):
    """One exchange in a conversation: a user message plus the assistant reply/replies.

    `turn_index` is dense (0, 1, 2, …), assigned `max(turn_index) + 1` under the
    conversation row lock (`get_conversation(..., for_update=True)`): concurrent
    posts to the same conversation serialise on that lock, so indexes never
    collide — the unique composite index is the belt-and-braces guard. Turns are
    never reordered (unlike `Scenario.position`), so the unique constraint is safe.
    """

    __tablename__ = "turns"
    __table_args__ = (
        # Serves `conversation_id`-prefix lookups + `ORDER BY turn_index`, and
        # guards uniqueness of the dense index per conversation.
        Index("ix_turns_conversation_id_turn_index", "conversation_id", "turn_index", unique=True),
    )

    conversation_id: uuid.UUID = Field(foreign_key="conversations.id", nullable=False, ondelete="CASCADE")
    turn_index: int = Field(nullable=False)


# The `Message.extra` key holding the tag map a reply's prompt carried. Named next to the column it
# lives in, because the writer (`persist_stream`) and the projection (`MessageBase`) must agree on it.
TAG_CONTEXT_EXTRA_KEY = "tag_context"

# The `Message.extra` key marking a stamped `tag_context` as covering only a `continue`'s
# continuation, not the whole reply. Present only alongside a non-empty `TAG_CONTEXT_EXTRA_KEY`.
TAG_CONTEXT_PARTIAL_EXTRA_KEY = "tag_context_partial"


def recorded_tag_context(message: Message) -> dict[str, str] | None:
    """The tag map this reply's prompt carried, or `None` when it recorded none.

    Every reader goes through here, so the writer/reader agreement the two keys above exist for has
    one home. `None` is not an empty map: it means undecidable (a user message, a reply older than
    the record, or a finalize that never ran), which is what makes the current policy the only
    available answer for that row.
    """
    record = message.extra.get(TAG_CONTEXT_EXTRA_KEY)
    return dict(record) if record is not None else None


class Message(BaseModel, table=True):
    """A single message within a `Turn` — user prompt, assistant reply, or system note.

    `status` (`MessageStatus`) is the generation lifecycle, orthogonal to
    `deleted_at`. An assistant reply is inserted as a `streaming` placeholder with
    empty `content` and finalised in place via a guarded
    `UPDATE ... WHERE status = 'streaming'`. `slot` labels the branch in
    dual/multi-model mode (`a`, `b`, …; null for user/system). `client_message_id`
    makes the user's submit idempotent (partial-unique while not null).
    Regenerate/continue insert a *new* message pointing at the superseded one via
    `replaces_message_id`; the live version is the one nothing points at, so
    history stays logically append-only.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # Idempotent user submit: a repeated client_message_id replays the turn
        # instead of duplicating it. Partial — only non-null ids are constrained.
        Index(
            "ix_messages_client_message_id",
            "client_message_id",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
        ),
        # Cheap scan for the reaper sweeping placeholders stuck in `streaming`
        # (hard crash): the partial keeps the index to just the live placeholders.
        Index("ix_messages_streaming", "created_at", postgresql_where=text("status = 'streaming'")),
        # Linear history: a message has at most one successor. Partial-unique
        # DB-enforces it (mirrors the turn_index guard) — no forks / variant tree.
        Index(
            "ix_messages_replaces_message_id",
            "replaces_message_id",
            unique=True,
            postgresql_where=text("replaces_message_id IS NOT NULL"),
        ),
    )

    turn_id: uuid.UUID = Field(foreign_key="turns.id", nullable=False, ondelete="CASCADE", index=True)
    role: MessageRole = Field(
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            MessageRole,
            values_callable=lambda enum: [m.value for m in enum],
            name="messagerole",
        ),
        sa_column_kwargs={"nullable": False},
    )
    status: MessageStatus = Field(
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            MessageStatus,
            values_callable=lambda enum: [m.value for m in enum],
            name="messagestatus",
        ),
        sa_column_kwargs={"nullable": False},
    )
    content: str = Field(default="", sa_type=Text, sa_column_kwargs={"nullable": False})
    # Whether `content` holds ciphertext rather than the text itself. Per row, not per conversation:
    # a protected conversation still stores empty text as-is (`seal_content` leaves it unsealed),
    # so its placeholders and error rows are unsealed while its real messages are not.
    content_encrypted: bool = Field(
        default=False,
        sa_column_kwargs={"nullable": False, "server_default": text("false")},
    )
    # Branch label in dual/multi-model mode (`a`, `b`, …); null for user/system.
    slot: str | None = Field(default=None, max_length=8)
    client_message_id: uuid.UUID | None = Field(default=None)
    # Self-FK: regenerate/continue points the new message at the one it supersedes.
    # History is linear — at most one successor per message (guarded by the
    # partial-unique index in __table_args__). SET NULL on a hard delete of the
    # superseded row keeps the survivor valid.
    replaces_message_id: uuid.UUID | None = Field(default=None, foreign_key="messages.id", ondelete="SET NULL")
    # Per-message generation metadata (finish_reason, usage, error, tag_context) — populated by
    # the streaming finalize. (`metadata` is reserved by SQLAlchemy, hence `extra`.)
    extra: dict[str, Any] = Field(
        default_factory=dict,
        sa_type=JSONB,
        sa_column_kwargs={"nullable": False, "server_default": text("'{}'::jsonb")},
    )
    # Free-form key/value tags attached to this (user) message, folded into the model's
    # `system_prompt` for that request alongside the conversation's tags. NOT NULL, default none.
    tags: dict[str, str] = Field(
        default_factory=dict,
        sa_type=JSONB,
        sa_column_kwargs={"nullable": False, "server_default": text("'{}'::jsonb")},
    )


MESSAGE_ROLE_ORDER = case(
    {MessageRole.USER: 0, MessageRole.SYSTEM: 1, MessageRole.ASSISTANT: 2},
    value=col(Message.role),
    else_=9,
)
"""Intra-turn sort key: user prompt first, then assistant reply(ies).

`created_at` cannot order a turn's messages — the prompt and its reply are written
in one transaction, so `now()` gives them the same value to the microsecond and the
`id` tiebreak (uuid4) decides. Every reader of a message selection pairs this with
`slot`, then `id`. A role absent from the map sorts last, so the map must grow with
`MessageRole`.
"""


class MessageImage(SQLModel, table=True):
    """Ordered image attachments of a user message — one row per attached image.

    A plain association row — **not** a `BaseModel` (mirrors `FlaggedMessage`): its
    lifecycle is bound to the message, so no `id` / timestamps / `deleted_at`.
    Decoupled from media like every other consumer: no FK to `media_assets`, just the
    opaque storage key. `position` preserves the user's attachment order — the order
    the parts are replayed to the model on every (re)dispatch.
    """

    __tablename__ = "message_images"

    message_id: uuid.UUID = Field(foreign_key="messages.id", primary_key=True, ondelete="CASCADE")
    position: int = Field(primary_key=True)
    image_key: str = Field(max_length=1024)
