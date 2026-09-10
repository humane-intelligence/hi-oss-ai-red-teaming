"""Annotation-domain tables — `MessageFlag`, `Note`, `Annotation`, `AnnotationLabel`, and the link rows.

A `MessageFlag` marks a selection of one conversation's messages as
exploit-worthy from the chat UI. It is anchored to a single `conversation_id`
and references an explicit **set** of that conversation's messages through the
`flagged_messages` join table — so a user can flag any subset (one message, a
contiguous range, or the whole conversation), and a conversation can carry many
independent flags. Selection is a set, not a contiguous range: `FlaggedMessage`
rows can name any messages of the conversation.

The ancestry beyond the conversation (`evaluation_id`, `evaluation_group_id`,
`scenario_id`) is **denormalised** from the `conversation → evaluation → group`
chain at create time: the create service resolves them under the caller's
ownership/visibility, so they can't drift. The payoff is that every read
(`_scope`, list filters, the future review queue) needs only the flag's own
columns. Reads still join the denormalised `conversation_id` / `evaluation_id`
through their live parents, so soft-deleting any ancestor (including the
conversation cascades wired for model-unassign/delete) hides the flag without a
dedicated cascade hook.

`status` is the review verdict; it lands `pending` and is moved by the review
workflow, which is a separate scope (no transition endpoint here yet). The
reviewer relation likewise belongs to that scope.

`Note` + `noted_messages` mirror that shape — including the denormalised ancestry
and the live-parent join above — for a lighter, independent note: it carries no
review status and its author need not own the conversation.

`Annotation` is the per-message label: one row = one (message, label, author),
anchored to a single `message_id` rather than a selection, with the same
denormalised ancestry and live-parent join as the entities above. It always
references an `AnnotationLabel` — curated, or one its author named.

`AnnotationLabel` shares none of that: it is flat reference data, anchored to
nothing, and is the odd one out in this module on purpose — it belongs to the
label vocabulary rather than to any one conversation.
"""

import uuid

from sqlalchemy import CheckConstraint
from sqlalchemy import Enum
from sqlalchemy import Index
from sqlalchemy import Text
from sqlalchemy import text
from sqlmodel import Field
from sqlmodel import Relationship
from sqlmodel import SQLModel
from sqlmodel import col

from app.core.annotations.enums import FlagStatus
from app.core.base_model import BaseModel
from app.core.conversations.models import MESSAGE_ROLE_ORDER
from app.core.conversations.models import Message

# Sort key shared by every message-selection projection here. `created_at` alone is
# not enough: a turn's prompt and reply are written in one transaction and share it
# to the microsecond, so the `id` (uuid4) tiebreak would order a selected exchange
# at random — and against the transcript endpoint, which reads role/`slot`/`id`.
_MESSAGE_SELECTION_ORDER = [
    col(Message.created_at),
    MESSAGE_ROLE_ORDER,
    col(Message.slot),
    col(Message.id),
]


class MessageFlag(BaseModel, table=True):
    __tablename__ = "message_flags"
    __table_args__ = (
        # The review queue and analytics filter by these denormalised parents.
        Index("ix_message_flags_conversation_id", "conversation_id"),
        Index("ix_message_flags_evaluation_id", "evaluation_id"),
        Index("ix_message_flags_evaluation_group_id", "evaluation_group_id"),
    )

    reason: str = Field(sa_type=Text, sa_column_kwargs={"nullable": False})
    # The red-teamer's assertion that the selection is exploit-worthy.
    red_flagged: bool = Field(
        default=True,
        sa_column_kwargs={"nullable": False, "server_default": text("true")},
    )
    # Optional free-text note from the author (distinct from a `Message`).
    comment: str | None = Field(default=None, sa_type=Text)
    status: FlagStatus = Field(
        default=FlagStatus.PENDING,
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            FlagStatus,
            values_callable=lambda enum: [m.value for m in enum],
            name="flagstatus",
        ),
        sa_column_kwargs={"nullable": False, "index": True, "server_default": text("'pending'")},
    )
    created_by_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)

    # The flagged conversation — the anchor. The remaining ancestry is denormalised.
    conversation_id: uuid.UUID = Field(foreign_key="conversations.id", nullable=False, ondelete="CASCADE")
    evaluation_id: uuid.UUID = Field(foreign_key="evaluations.id", nullable=False, ondelete="CASCADE")
    evaluation_group_id: uuid.UUID = Field(foreign_key="evaluation_groups.id", nullable=False, ondelete="CASCADE")
    # Soft-deleted parents: scenarios/tasks soft-delete, so `SET NULL` only fires
    # on a hard delete — it records which challenge/task the attempt targeted.
    # Note: scenario/task liveness is not re-checked on read, so the `scenario_id`
    # filter can still match a flag whose scenario was later soft-deleted —
    # accepted, since the flag stays valid under its (live) conversation/group.
    scenario_id: uuid.UUID | None = Field(default=None, foreign_key="scenarios.id", ondelete="SET NULL", index=True)
    # Optional link to a specific task of the scenario (the "dropdown with Tasks").
    task_id: uuid.UUID | None = Field(default=None, foreign_key="tasks.id", ondelete="SET NULL", index=True)

    # The selected messages, projected straight through the `flagged_messages`
    # link table. `viewonly` because the link rows are written directly (see the
    # service) — this side is read-only, so it never conflicts with those writes
    # and emits no overlap warning. Eager-load with `selectinload(...)` +
    # `with_live(Message)`, which keeps the page one extra query; the liveness
    # filter is belt-and-braces, since nothing soft-deletes a `Message`.
    # Ordered oldest-first across turns, then by the transcript's intra-turn key.
    messages: list[Message] = Relationship(
        sa_relationship_kwargs={
            "secondary": "flagged_messages",
            "order_by": _MESSAGE_SELECTION_ORDER,
            "viewonly": True,
        },
    )


class TaskCompletion(BaseModel, table=True):
    """A red-teamer's check-off of one scenario task within one conversation.

    Truth lives **per conversation** (single-owner via `Conversation.user_id`, no
    participants table — so a completion is per-user by construction; `created_by_id`
    is attribution, not part of the key); the group-level route only rolls those rows
    up as "K/N".

    Ancestry beyond the conversation is **denormalised** from the
    `conversation → evaluation → group` chain at create (resolved once under the
    caller's ownership/visibility so it can't drift). Unlike `MessageFlag`, a
    completion always names a concrete `task_id` and needs its group for the roll-up,
    so `task_id` / `conversation_group_id` are **NOT NULL**; `scenario_id` stays
    nullable `SET NULL` so a soft/hard-deleted scenario can't wedge the row.

    No "completed" column — **a live row means done**; toggling off soft-deletes it,
    so `created_at` is the check-off time and the soft-delete trail is the history
    (re-checking a freed pair inserts a new row). The partial-unique
    `(conversation_id, task_id)` index keeps at most one live completion per pair.
    """

    __tablename__ = "task_completions"
    __table_args__ = (
        # At most one live completion per (conversation, task); soft-delete frees
        # the pair so it can be re-checked (a fresh row, fresh created_at).
        Index(
            "ix_task_completions_conversation_id_task_id",
            "conversation_id",
            "task_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # The group roll-up and future analytics scope by these denormalised parents.
        Index("ix_task_completions_conversation_group_id", "conversation_group_id"),
        Index("ix_task_completions_evaluation_id", "evaluation_id"),
        Index("ix_task_completions_evaluation_group_id", "evaluation_group_id"),
    )

    created_by_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)

    # The conversation the task was checked off in — the anchor.
    conversation_id: uuid.UUID = Field(foreign_key="conversations.id", nullable=False, ondelete="CASCADE")
    # The completed task — a completion always names one (unlike a flag's optional task).
    task_id: uuid.UUID = Field(foreign_key="tasks.id", nullable=False, ondelete="CASCADE")
    # Denormalised ancestry (resolved once at create). The group is required — the
    # roll-up scopes by it — so unlike MessageFlag it is carried and NOT NULL.
    conversation_group_id: uuid.UUID = Field(foreign_key="conversation_groups.id", nullable=False, ondelete="CASCADE")
    evaluation_id: uuid.UUID = Field(foreign_key="evaluations.id", nullable=False, ondelete="CASCADE")
    evaluation_group_id: uuid.UUID = Field(foreign_key="evaluation_groups.id", nullable=False, ondelete="CASCADE")
    # The task's scenario, present at create (a task belongs to a scenario). `SET
    # NULL` on a hard delete only — scenarios soft-delete, so it normally survives.
    scenario_id: uuid.UUID | None = Field(default=None, foreign_key="scenarios.id", ondelete="SET NULL", index=True)


class Note(BaseModel, table=True):
    """An annotator's free-text note on a selection of one conversation's messages.

    Independent of `MessageFlag`: a note stands whether or not the participant
    flagged the response, carries no review status, and its author need not own the
    conversation. The module docstring above states the ancestry invariant this
    shares with `MessageFlag`; the access policy behind it lives with the code that
    enforces it, in `services/notes.py`.

    The selection is a set, not a contiguous range, and is fixed at create: a
    different selection is a new note, never an edit of this one.
    """

    __tablename__ = "notes"
    __table_args__ = (
        # The flat list and its filters scope by these denormalised parents.
        Index("ix_notes_conversation_id", "conversation_id"),
        Index("ix_notes_evaluation_id", "evaluation_id"),
        Index("ix_notes_evaluation_group_id", "evaluation_group_id"),
    )

    # Shadows the module-level `text` import for the rest of this class body: a
    # `server_default=text(...)` added below would call the `Field`, not the SQL
    # helper. Import it under an alias here if one is ever needed.
    text: str = Field(sa_type=Text, sa_column_kwargs={"nullable": False})
    # No delete rule: users soft-delete, so a hard delete is refused and authorship
    # cannot silently vanish from the note.
    created_by_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)

    # The noted conversation — the anchor. The remaining ancestry is denormalised.
    conversation_id: uuid.UUID = Field(foreign_key="conversations.id", nullable=False, ondelete="CASCADE")
    evaluation_id: uuid.UUID = Field(foreign_key="evaluations.id", nullable=False, ondelete="CASCADE")
    evaluation_group_id: uuid.UUID = Field(foreign_key="evaluation_groups.id", nullable=False, ondelete="CASCADE")

    # The selected messages, projected through the `noted_messages` link table.
    # `viewonly` because the link rows are written directly by the create service —
    # this side never conflicts with those writes and emits no overlap warning.
    # Eager-load with `selectinload(...)` + `with_live(Message)`, which keeps the
    # page one extra query; the liveness filter is belt-and-braces, since nothing
    # soft-deletes a `Message`. Ordered oldest-first across turns, then by the
    # transcript's intra-turn key.
    messages: list[Message] = Relationship(
        sa_relationship_kwargs={
            "secondary": "noted_messages",
            "order_by": _MESSAGE_SELECTION_ORDER,
            "viewonly": True,
        },
    )


# Declared before `Annotation`, load-bearing: that class annotates its relationship with the
# class object (`label: AnnotationLabel`) and this module has no `from __future__ import
# annotations`, so the name must already exist when the body executes — swapping the two is an
# immediate `NameError`, not a lazy mapper failure.
class AnnotationLabel(BaseModel, table=True):
    """One label the annotation picker can offer — curated, or one an annotator named.

    Two kinds, distinguished by `created_by_id` exactly as `DataLicense` distinguishes its
    own. **Curated** (`NULL`) is the shared vocabulary shipped in `label_catalog.py` and
    reconciled by `sync_annotation_labels`, keyed by `key` so rewording an entry keeps
    whatever points at it. **User-scoped** (`created_by_id` set, no `key`) is a label an
    annotator named while annotating, so they are not made to retype it — it never enters
    the shared list.

    Both kinds have a writer: the curated sync, and `find_or_create_user_label` on the
    annotate path — which resolves a wording to the curated row, then the caller's own, before
    minting anything, so a spelling is one row wherever the catalog holds it, and one row per author otherwise.

    Soft-deletable, so a tombstoned entry drops out of the picker without invalidating rows
    that reference it. Nothing tombstones a curated one today — the only path is a manual
    UPDATE, and the sync revives any row whose catalog entry remains — so durable retirement
    means removing the catalog entry *and* tombstoning by hand, until label CRUD or the
    taxonomy-swap migration owns it.
    """

    __tablename__ = "annotation_labels"
    __table_args__ = (
        # The two kinds, as a database fact rather than a convention: a curated row carries a
        # `key` and no author, a user row an author and no `key`. The partial indexes below
        # deliberately do not span the kinds, so without this a hybrid row (both, or neither)
        # would slip past every uniqueness rule and belong to neither vocabulary. It also keeps
        # `key` curated-only, which is what stops a future user-label writer from colliding
        # with a curated key that the id-keyed upsert cannot see.
        CheckConstraint("(created_by_id IS NULL) = (key IS NOT NULL)", name="kind_is_curated_xor_authored"),
        # Curated identity: one live row per key, so the key is an identity rather than a
        # label. Cross-kind collision is the CHECK's doing, not this predicate's — an
        # annotator's row cannot carry a key at all. Partial on live rows, so a retired key
        # can be re-added after a tombstone.
        Index(
            "ix_annotation_labels_key",
            "key",
            unique=True,
            postgresql_where=text("created_by_id IS NULL AND deleted_at IS NULL"),
        ),
        # Per-author identity: one entry per (author, folded name). `lower(name)` so naming
        # "Jailbreak" then "jailbreak" yields one label — folded in Postgres, never Python,
        # which disagree on dotted capitals.
        Index(
            "ix_annotation_labels_author_name",
            "created_by_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("created_by_id IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    # No delete rule: users soft-delete, so a hard delete is refused and authorship cannot
    # vanish from the label. Unlike `DataLicense`, `SET NULL` is not even available here — it
    # would leave a user row with neither an author nor a key, violating the CHECK above.
    created_by_id: uuid.UUID | None = Field(default=None, foreign_key="users.id", index=True)
    key: str | None = Field(default=None, max_length=64)
    name: str = Field(max_length=128, nullable=False)


class Annotation(BaseModel, table=True):
    """One label on one message from one author.

    The aggregable sibling of `Note`: a note is prose about a selection, an annotation is
    a label you count — hence one row per **(message, label, author)** and no free-form
    body. Every annotation references an `AnnotationLabel`, curated or the author's own:
    typing a new label creates that row first (see `services/annotation_labels`), so
    counting is always a join and never a string match, and export always has a label
    entity to point at.

    Denormalised ancestry and the live-parent join follow `Note`. One partial unique dedupes
    live rows over the whole triple — `created_by_id` is in it because each annotator labels
    independently, not as a tiebreak. Tagging two *spans* of one message with the same label
    would widen it further, with the span's offset columns.

    There is no editable content, so no update path exists anywhere — changing the label
    is delete + re-create, and the row's identity is fixed at insert.
    """

    __tablename__ = "annotations"
    __table_args__ = (
        # Dedup among live rows: one (message, label, author) triple at a time. Case folding
        # lives on the label row now, so this needs no expression — a typed label resolves to
        # the author's single entry before it ever reaches here.
        Index(
            "ix_annotations_message_label_author",
            "message_id",
            "label_id",
            "created_by_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # The flat list and its filters scope by these denormalised parents.
        Index("ix_annotations_conversation_id", "conversation_id"),
        Index("ix_annotations_evaluation_id", "evaluation_id"),
        Index("ix_annotations_evaluation_group_id", "evaluation_group_id"),
    )

    # The annotated message. No delete rule: the database refuses to hard-delete an
    # annotated message directly (mirrors `NotedMessage.message_id`); a conversation
    # hard-delete still cascades cleanly, because this row dies via its own
    # `conversation_id` FK in the same statement.
    message_id: uuid.UUID = Field(foreign_key="messages.id", nullable=False, index=True)
    # The label carried, curated or the author's own. No delete rule: a referenced label
    # cannot be hard-deleted — retiring one is a soft delete, and this row keeps resolving it.
    label_id: uuid.UUID = Field(foreign_key="annotation_labels.id", nullable=False, index=True)
    # No delete rule: users soft-delete, so a hard delete is refused and authorship
    # cannot silently vanish from the annotation.
    created_by_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)

    # The anchor's conversation plus denormalised ancestry, resolved once at create.
    conversation_id: uuid.UUID = Field(foreign_key="conversations.id", nullable=False, ondelete="CASCADE")
    evaluation_id: uuid.UUID = Field(foreign_key="evaluations.id", nullable=False, ondelete="CASCADE")
    evaluation_group_id: uuid.UUID = Field(foreign_key="evaluation_groups.id", nullable=False, ondelete="CASCADE")

    # Eager-load with `selectinload(...)` and deliberately **without** `with_live(...)`:
    # a retired (soft-deleted) label leaves the picker but must keep rendering on the
    # rows that reference it — filtering liveness here would blank them.
    label: AnnotationLabel = Relationship()


class FlaggedMessage(SQLModel, table=True):
    """Association row linking one `MessageFlag` to one selected `Message`.

    A plain link table — **not** a `BaseModel`: its lifecycle is bound to the
    parent flag (the set is replaced wholesale, never independently
    soft-deleted), so it carries no `id` / timestamps / `deleted_at`. The
    composite PK `(message_flag_id, message_id)` both dedupes the selection and
    serves the `message_flag_id`-prefix load; the extra `message_id` index serves
    the reverse "which flags include this message?" filter.

    `message_flag_id` is `ON DELETE CASCADE` — dropping a flag drops its links.
    `message_id` deliberately is **not**: with no cascade the FK is `NO ACTION`,
    so the database refuses to hard-delete a message that any flag references —
    the DB-level half of "a flagged message can't be deleted".
    """

    __tablename__ = "flagged_messages"
    __table_args__ = (Index("ix_flagged_messages_message_id", "message_id"),)

    message_flag_id: uuid.UUID = Field(foreign_key="message_flags.id", primary_key=True, ondelete="CASCADE")
    message_id: uuid.UUID = Field(foreign_key="messages.id", primary_key=True)


class NotedMessage(SQLModel, table=True):
    """Association row linking one `Note` to one selected `Message`.

    A plain link table — **not** a `BaseModel`: its lifecycle is bound to the parent
    note (the selection is fixed at create, never independently soft-deleted), so it
    carries no `id` / timestamps / `deleted_at`. The composite PK
    `(note_id, message_id)` both dedupes the selection and serves the
    `note_id`-prefix load; the extra `message_id` index serves the reverse
    "which notes include this message?" filter.

    `note_id` is `ON DELETE CASCADE` — dropping a note drops its links.
    `message_id` deliberately is **not**: with no cascade the FK is `NO ACTION`, so
    the database refuses to hard-delete a message any note references.
    """

    __tablename__ = "noted_messages"
    __table_args__ = (Index("ix_noted_messages_message_id", "message_id"),)

    note_id: uuid.UUID = Field(foreign_key="notes.id", primary_key=True, ondelete="CASCADE")
    message_id: uuid.UUID = Field(foreign_key="messages.id", primary_key=True)
