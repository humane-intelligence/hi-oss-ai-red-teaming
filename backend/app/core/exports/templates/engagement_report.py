"""The "engagement report" CSV export — one client-facing row per conversation.

A denormalized, shareable view (the shape used for client deliverables): red-teamer
email + the evaluation-group / evaluation titles + the conversation's full message list
as a JSON array. Unlike `transcript` (one normalized row per message), this keeps a whole
conversation on a single row — every embedded message carries its `message_id`, `turn_id`,
`role`, `status`, `content` and its own `tags` (the shared `message_dicts` shape); for full-fidelity
reconstruction (replaced-message chain, slot) use the `transcript` export.

Scope is one evaluation (so the group/evaluation columns are constant across the file);
spanning many evaluation groups would need a broader export scope than the per-evaluation endpoint.

Row-level scope is group-wide: every authorised export (only the group's owner or a break-glass
admin gets past the endpoint gate) contains the whole evaluation's conversations — every
red-teamer's, with their emails — so the report is a complete client deliverable, not just the
exporter's own rows. Non-owners can't export at all, so this is under no obligation to redact.
See `ExportScope`.
"""

import json
from collections.abc import AsyncIterator
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.roles import Permission
from app.core.config import get_settings
from app.core.conversations.models import Message
from app.core.conversations.models import recorded_tag_context
from app.core.csv_generator import Column
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.services.evaluations import get_evaluation
from app.core.evaluations.services.evaluations import resolve_effective_license
from app.core.evaluations.services.tag_keys import TagFoldPolicy
from app.core.evaluations.services.tag_keys import load_tag_fold_policy
from app.core.exports.base import CsvExport
from app.core.exports.base import ExportScope
from app.core.exports.conversation_messages import iter_scoped_conversations
from app.core.exports.conversation_messages import message_dicts
from app.core.exports.lookups import resolve_user_emails


@dataclass(frozen=True, slots=True)
class EngagementReportRow:
    """One conversation denormalized with its engagement context and message blob."""

    redteamer_email: str | None
    evaluation_group_id: UUID
    evaluation_group_title: str | None
    evaluation_id: UUID
    evaluation_title: str
    data_license: str
    conversation_id: UUID
    started_at: datetime
    # The conversation-level prompt context (the per-message layer rides inside `messages`), plus the
    # keys that did not reach the prompt — judged against a turn's own record where it has one, and
    # against the current policy otherwise. A client deliverable has to be able to say what the model
    # is told, and what it is not.
    conversation_tags: dict[str, str]
    tags_not_sent: list[str]
    # Raw message list (not pre-serialized): the CSV column's `formatter` JSON-encodes it to a
    # string cell, while the JSON export emits it as a real nested array via the column's `value`.
    messages: list[dict[str, str | dict[str, str] | None]]


def _unsent_across_turns(
    fold: TagFoldPolicy,
    conversation_tags: dict[str, str],
    messages: Iterable[Message],
) -> list[str]:
    """Keys authored on the row that no turn ever sent, sorted.

    One row covers a whole conversation, so a per-turn property has to be reduced: a key counts as sent
    once *some* turn sent it, and is reported only if none did. Merging every message map into one
    instead would let the last message decide — a later turn blanking `env` would claim `env` never
    reached the model, though an earlier turn sent it. A turn whose reply carries its own `tag_context`
    record is judged historically against it (`unsent_for_message`); the rest fall back to the current
    policy — same distinction as the `transcript` export, applied per turn instead of per row.
    """
    # Grouped by turn, not by message: only the user message of a turn carries tags, so treating the
    # assistant placeholder's empty map as its own turn would judge the conversation layer alone.
    by_turn: dict[UUID, dict[str, str]] = {}
    record_holder_by_turn: dict[UUID, Message] = {}
    for message in messages:
        by_turn.setdefault(message.turn_id, {}).update(message.tags)
        if recorded_tag_context(message) is not None:
            record_holder_by_turn[message.turn_id] = message
    # Accumulating what *was* sent, rather than intersecting what wasn't: a turn that simply has no key
    # `k` reports an unsent set without it, so an intersection would read that absence as "this turn
    # sent k" and cancel every other turn's report of it — silently emptying the column exactly when
    # the layers differ per turn. `by_turn or {None: {}}` covers a conversation with no turns at all,
    # where its own map is the only thing to judge.
    keys: set[str] = set(conversation_tags)
    sent: set[str] = set()
    for turn_id, turn_tags in (by_turn or {None: {}}).items():
        merged = {**conversation_tags, **turn_tags}
        keys |= set(merged)
        record_holder = record_holder_by_turn.get(turn_id)
        if record_holder is not None:
            turn_unsent = fold.unsent_for_message(record_holder, merged)
        else:
            turn_unsent = fold.unsent(merged)
        sent |= set(merged) - set(turn_unsent)
    return sorted(keys - sent)


async def _fetch_engagement_report(session: AsyncSession, scope: ExportScope) -> AsyncIterator[EngagementReportRow]:
    can_manage = scope.full_group_access
    # The caller (both export endpoints) already loaded the visibility-checked evaluation;
    # reuse it rather than re-issuing get_evaluation per row in the group export. The parent
    # group is live (the evaluation wouldn't be visible otherwise), so a plain `get` is
    # enough for its title.
    evaluation = scope.evaluation or await get_evaluation(
        session, scope.evaluation_id, caller_id=scope.caller.id, can_manage=can_manage
    )
    group = await session.get(EvaluationGroup, evaluation.evaluation_group_id)
    # The effective data license (evaluation override → group override → platform default), embedded on
    # every row so the deliverable carries its license metadata. `stream_export` batch-resolves
    # it onto the scope for the whole group; fall back to the single-id resolver only for a scope
    # built directly (e.g. a test).
    lic = (
        scope.effective_license
        if scope.effective_license is not None
        else await resolve_effective_license(session, scope.evaluation_id)
    )
    data_license = lic.spdx_id or lic.name

    # Stream the evaluation's visible conversations + live messages one page at a time;
    # owner emails are resolved per page (a bounded batch), so peak memory stays ~one page.
    fold = await load_tag_fold_policy(session, scope.evaluation_id)  # once per export, not per row
    async for conversations, messages_by_conversation in iter_scoped_conversations(session, scope):
        emails = await resolve_user_emails(session, (conversation.user_id for conversation in conversations))
        for conversation in conversations:
            # Shared embedded-message shape (see `message_dicts`): a real nested array in JSON, a
            # JSON-string cell in CSV. `status` keeps an interrupted/streaming placeholder (empty
            # `content`) distinguishable from a genuine empty reply; for full-fidelity reconstruction
            # (replaced-message chain, slot) use the transcript export.
            raw_messages = messages_by_conversation.get(conversation.id, [])
            messages = message_dicts(raw_messages, settings=get_settings())
            yield EngagementReportRow(
                redteamer_email=emails.get(conversation.user_id),
                evaluation_group_id=evaluation.evaluation_group_id,
                evaluation_group_title=group.title if group is not None else None,
                evaluation_id=evaluation.id,
                evaluation_title=evaluation.title,
                data_license=data_license,
                conversation_id=conversation.id,
                started_at=conversation.created_at,
                conversation_tags=dict(conversation.tags),
                # The row carries both layers (the per-message one inside `messages`), so the column has
                # to judge both — and per turn, then reduced; see `_unsent_across_turns`.
                tags_not_sent=_unsent_across_turns(fold, dict(conversation.tags), raw_messages),
                messages=messages,
            )


_COLUMNS: list[Column[EngagementReportRow]] = [
    Column("Red-teamer email", lambda r: r.redteamer_email),
    Column("Evaluation group ID", lambda r: r.evaluation_group_id),
    Column("Evaluation group title", lambda r: r.evaluation_group_title),
    Column("Evaluation ID", lambda r: r.evaluation_id),
    Column("Evaluation title", lambda r: r.evaluation_title),
    Column("Data license", lambda r: r.data_license),
    Column("Conversation ID", lambda r: r.conversation_id),
    Column("Started at", lambda r: r.started_at),
    Column(
        "Conversation tags",
        value=lambda r: r.conversation_tags,
        formatter=lambda v: json.dumps(v, ensure_ascii=False),
    ),
    Column(
        "Tags not sent",
        value=lambda r: r.tags_not_sent,
        formatter=lambda v: json.dumps(v, ensure_ascii=False),
    ),
    # `value` is the raw list (real nested array in JSON); `formatter` JSON-encodes it for the CSV cell.
    Column("Messages", value=lambda r: r.messages, formatter=lambda v: json.dumps(v, ensure_ascii=False)),
]


ENGAGEMENT_REPORT_EXPORT: CsvExport[EngagementReportRow] = CsvExport(
    key="engagement_report",
    name="Engagement report",
    description=(
        "One client-facing row per conversation — red-teamer email, evaluation-group / evaluation titles, the "
        "conversation's model-facing tags and 'Tags not sent' (historical against a turn's recorded "
        "tag_context where one exists, current-policy otherwise), "
        "and the full transcript as a JSON array where each message carries its message_id, turn_id, role, "
        "status, content and tags; use the transcript export for full-fidelity reconstruction "
        "(replaced-message chain, slot)."
    ),
    permission=Permission.CONVERSATIONS_READ,
    columns=_COLUMNS,
    fetch=_fetch_engagement_report,
    needs_effective_license=True,
    supported_filters=frozenset({"created_from", "created_to", "scenario_id", "user_id"}),
)
