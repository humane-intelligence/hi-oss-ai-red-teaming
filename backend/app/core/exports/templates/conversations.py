"""The "conversations" CSV export — one row per `Conversation` in an evaluation."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.roles import Permission
from app.core.config import get_settings
from app.core.conversations.filters import ConversationFilters
from app.core.conversations.models import Conversation
from app.core.conversations.services.conversations import list_conversations
from app.core.csv_generator import Column
from app.core.evaluations.services.tag_keys import load_tag_fold_policy
from app.core.exports.base import CsvExport
from app.core.exports.base import ExportScope
from app.core.exports.base import iter_pages
from app.core.exports.lookups import resolve_scenario_names
from app.core.exports.lookups import resolve_scope_evaluation_title
from app.core.exports.lookups import resolve_user_emails
from app.core.restore import restore_cutoff


@dataclass(frozen=True, slots=True)
class ConversationRow:
    """A conversation plus the resolved evaluation title, owner email, and scenario name."""

    conversation: Conversation
    evaluation_title: str | None
    owner_email: str | None
    scenario_name: str | None
    # Keys of the conversation's stored tag map the evaluation's tagging policy currently keeps out of
    # the prompt (see `TagFoldPolicy`) — a statement about the policy now, not about past turns.
    tags_not_sent: list[str]


async def _fetch_conversations(session: AsyncSession, scope: ExportScope) -> AsyncIterator[ConversationRow]:
    can_manage = scope.full_group_access
    evaluation_title = await resolve_scope_evaluation_title(session, scope)  # constant across the export
    # Conversations honor scenario / user (owner) / date-range; status + task don't apply here.
    ef = scope.filters
    conversation_filters = ConversationFilters(
        evaluation_id=scope.evaluation_id,
        scenario_id=ef.scenario_id,
        user_id=ef.user_id,
        created_from=ef.created_from,
        created_to=ef.created_to,
    )
    fold = await load_tag_fold_policy(session, scope.evaluation_id)  # once per export, not per row
    async for conversations in iter_pages(
        lambda limit, offset: list_conversations(
            session,
            caller_id=scope.caller.id,
            can_manage=can_manage,
            filters=conversation_filters,
            order_by="-created_at",
            limit=limit,
            offset=offset,
            deleted_cutoff=restore_cutoff(get_settings()),
        )
    ):
        emails = await resolve_user_emails(session, (c.user_id for c in conversations))
        scenarios = await resolve_scenario_names(session, (c.scenario_id for c in conversations))
        for conversation in conversations:
            yield ConversationRow(
                conversation=conversation,
                evaluation_title=evaluation_title,
                owner_email=emails.get(conversation.user_id),
                scenario_name=scenarios.get(conversation.scenario_id) if conversation.scenario_id else None,
                tags_not_sent=fold.unsent(conversation.tags),
            )


_COLUMNS: list[Column[ConversationRow]] = [
    Column("Conversation ID", lambda r: r.conversation.id),
    Column("Evaluation ID", lambda r: r.conversation.evaluation_id),
    Column("Evaluation title", lambda r: r.evaluation_title),
    Column("Model assignment ID", lambda r: r.conversation.evaluation_ai_model_id),
    Column("Scenario ID", lambda r: r.conversation.scenario_id),
    Column("Scenario name", lambda r: r.scenario_name),
    Column("Group ID", lambda r: r.conversation.conversation_group_id),
    Column("Owner ID", lambda r: r.conversation.user_id),
    Column("Owner email", lambda r: r.owner_email),
    # The conversation's tags ride along on every turn as prompt context, so they belong in the
    # row — as stored, since the fold drops any key the evaluation's tagging policy no longer allows:
    # a real nested object in the JSON export, a JSON-encoded cell in CSV.
    Column(
        "Tags",
        value=lambda r: dict(r.conversation.tags),
        formatter=lambda v: json.dumps(v, ensure_ascii=False),
    ),
    # `Tags` is the stored map; this names the keys the policy keeps out of the prompt, so a reader is
    # not left assuming every stored tag reaches the model.
    Column(
        "Tags not sent",
        value=lambda r: r.tags_not_sent,
        formatter=lambda v: json.dumps(v, ensure_ascii=False),
    ),
    Column("Created", lambda r: r.conversation.created_at),
]


CONVERSATIONS_EXPORT: CsvExport[ConversationRow] = CsvExport(
    key="conversations",
    name="Conversations",
    description=(
        "One row per conversation in the evaluation — evaluation (with title), model assignment, scenario (with "
        "name), group, owner (with email), the conversation's tags as stored (with which of their keys "
        "the evaluation's tagging policy currently keeps out of the prompt) — prompt context, "
        "minus any the evaluation's tagging policy no longer allows, which stay on the row but are "
        "not sent."
    ),
    permission=Permission.CONVERSATIONS_READ,
    columns=_COLUMNS,
    fetch=_fetch_conversations,
    supported_filters=frozenset({"created_from", "created_to", "scenario_id", "user_id"}),
)
