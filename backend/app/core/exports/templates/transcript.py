"""The "transcript" CSV export — one row per message across all conversations in an evaluation.

Flattens every visible conversation's live messages into a single CSV, each row carrying its
conversation id for context. Conversations come from the owner/visibility-scoped
`list_conversations`; their messages are then read directly (already authorised).
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.roles import Permission
from app.core.config import get_settings
from app.core.conversations.models import TAG_CONTEXT_PARTIAL_EXTRA_KEY
from app.core.conversations.models import Message
from app.core.conversations.models import recorded_tag_context
from app.core.csv_generator import Column
from app.core.evaluations.services.evaluations import resolve_effective_license
from app.core.evaluations.services.tag_keys import load_tag_fold_policy
from app.core.exports.base import CsvExport
from app.core.exports.base import ExportScope
from app.core.exports.conversation_messages import iter_scoped_conversations
from app.core.exports.conversation_messages import unseal_for_export
from app.core.exports.lookups import resolve_scope_evaluation_title


@dataclass(frozen=True, slots=True)
class TranscriptRow:
    """One message paired with the conversation (and its evaluation) it belongs to."""

    evaluation_id: UUID
    evaluation_title: str | None
    data_license: str
    conversation_id: UUID
    conversation_tags: dict[str, str]
    message: Message
    content: str  # unsealed once when the row is built, so each column callable sees text
    # Keys of this row's tag map (the conversation's plus this message's own) the tagging policy
    # keeps out of the prompt — historical (from the message's own `tag_context` record) when it has
    # one, current-policy otherwise; see `TagFoldPolicy.unsent_for_message`.
    tags_not_sent: list[str]


async def _fetch_transcript(session: AsyncSession, scope: ExportScope) -> AsyncIterator[TranscriptRow]:
    # One row per live message, streamed page-by-page in the conversations' own `created_at`
    # order. Scope is a single evaluation, so its title resolves once (not per page/row).
    evaluation_title = await resolve_scope_evaluation_title(session, scope)
    # Effective data license (evaluation override → group override → platform default), embedded per row.
    # `stream_export` batch-resolves it onto the scope for the whole group; fall back to the
    # single-id resolver only for a scope built directly (e.g. a test).
    lic = (
        scope.effective_license
        if scope.effective_license is not None
        else await resolve_effective_license(session, scope.evaluation_id)
    )
    data_license = lic.spdx_id or lic.name
    # Resolved once per export (scope is one evaluation), not per row.
    fold = await load_tag_fold_policy(session, scope.evaluation_id)
    async for conversations, messages_by_conversation in iter_scoped_conversations(session, scope):
        for conversation in conversations:
            for message in messages_by_conversation.get(conversation.id, []):
                yield TranscriptRow(
                    evaluation_id=conversation.evaluation_id,
                    evaluation_title=evaluation_title,
                    data_license=data_license,
                    conversation_id=conversation.id,
                    conversation_tags=dict(conversation.tags),
                    message=message,
                    content=unseal_for_export(message, settings=get_settings()),
                    # The fold merges the two layers with the message's winning per key.
                    tags_not_sent=fold.unsent_for_message(message, {**conversation.tags, **message.tags}),
                )


_COLUMNS: list[Column[TranscriptRow]] = [
    # `Evaluation ID` (+ title) disambiguates rows in the whole-group export (unions evaluations).
    Column("Evaluation ID", lambda r: r.evaluation_id),
    Column("Evaluation title", lambda r: r.evaluation_title),
    Column("Data license", lambda r: r.data_license),
    Column("Conversation ID", lambda r: r.conversation_id),
    # `Message ID` + `Turn ID` + `Replaces message ID` are the ids that make the flat rows
    # reconstructable: group by `Turn ID`, order within a turn by role, then slot (rows already
    # arrive in that order — intra-turn `Created` is identical, so it's not a usable tie-break),
    # and follow `Replaces message ID` to see what a regenerate/continue superseded (the export
    # carries live messages only).
    Column("Message ID", lambda r: r.message.id),
    Column("Turn ID", lambda r: r.message.turn_id),
    Column("Replaces message ID", lambda r: r.message.replaces_message_id),
    Column("Role", lambda r: r.message.role),
    Column("Status", lambda r: r.message.status),
    Column("Content", lambda r: r.content),
    Column("Slot", lambda r: r.message.slot),
    # Both tag layers are prompt context (the conversation's on every turn, the message's on that
    # turn only), so a transcript without them can't explain what the model was told. Read as stored,
    # not as sent: the fold drops keys the evaluation's tagging policy no longer allows, so a column
    # here can name a tag that never reached the model. Real nested objects in the JSON export;
    # JSON-encoded one-cell strings in CSV.
    Column(
        "Conversation tags",
        value=lambda r: r.conversation_tags,
        formatter=lambda v: json.dumps(v, ensure_ascii=False),
    ),
    Column("Message tags", value=lambda r: dict(r.message.tags), formatter=lambda v: json.dumps(v, ensure_ascii=False)),
    # The tag map this reply's prompt actually carried (empty for a user message or an unrecorded
    # reply), plus the flag that says the map covers only a `continue`'s appended text — without it
    # the cell reads as the whole reply's context, while `Content` holds prefix + continuation and
    # the prefix's own record sits on a superseded row this export excludes.
    Column(
        "Tag context",
        value=lambda r: recorded_tag_context(r.message) or {},
        formatter=lambda v: json.dumps(v, ensure_ascii=False),
    ),
    Column(
        "Tag context partial",
        value=lambda r: bool(r.message.extra.get(TAG_CONTEXT_PARTIAL_EXTRA_KEY, False)),
    ),
    # Historical (judged against the row's own `Tag context` record) where one exists; current-policy
    # otherwise — see `TagFoldPolicy.unsent_for_message`.
    Column(
        "Tags not sent",
        value=lambda r: r.tags_not_sent,
        # JSON-encoded like the tag maps beside it: a bare join is ambiguous for a key containing a
        # comma and, starting with `-`, would be read as a formula by a spreadsheet.
        formatter=lambda v: json.dumps(v, ensure_ascii=False),
    ),
    Column("Created", lambda r: r.message.created_at),
]


TRANSCRIPT_EXPORT: CsvExport[TranscriptRow] = CsvExport(
    key="transcript",
    name="Transcript",
    description=(
        "One row per message across all conversations in the evaluation — carries the "
        "conversation, message, turn, and replaced-message ids so a conversation is fully "
        "reconstructable, plus role, content, status, both tag layers as stored — the conversation's "
        "and the message's own — the reply's recorded 'Tag context' with its 'Tag context partial' flag, "
        "and 'Tags not sent' (historical "
        "against that record where one exists, current-policy otherwise)."
    ),
    permission=Permission.CONVERSATIONS_READ,
    columns=_COLUMNS,
    fetch=_fetch_transcript,
    needs_effective_license=True,
    supported_filters=frozenset({"created_from", "created_to", "scenario_id", "user_id"}),
)
