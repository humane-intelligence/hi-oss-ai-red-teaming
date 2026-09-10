"""The "conversation-groups" CSV export — one row per `ConversationGroup` in an evaluation."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.roles import Permission
from app.core.conversations.filters import ConversationGroupFilters
from app.core.conversations.models import ConversationGroup
from app.core.conversations.services.groups import list_conversation_groups
from app.core.csv_generator import Column
from app.core.exports.base import CsvExport
from app.core.exports.base import ExportScope
from app.core.exports.base import iter_pages
from app.core.exports.lookups import resolve_scenario_names
from app.core.exports.lookups import resolve_scope_evaluation_title
from app.core.exports.lookups import resolve_user_emails


@dataclass(frozen=True, slots=True)
class ConversationGroupRow:
    """A conversation group plus the resolved evaluation title, owner email, and scenario name."""

    group: ConversationGroup
    evaluation_title: str | None
    owner_email: str | None
    scenario_name: str | None


async def _fetch_conversation_groups(session: AsyncSession, scope: ExportScope) -> AsyncIterator[ConversationGroupRow]:
    can_manage = scope.full_group_access
    evaluation_title = await resolve_scope_evaluation_title(session, scope)  # constant across the export
    # Conversation groups honor scenario / user (owner) / date-range; status / task don't apply here.
    ef = scope.filters
    group_filters = ConversationGroupFilters(
        evaluation_id=scope.evaluation_id,
        scenario_id=ef.scenario_id,
        user_id=ef.user_id,
        created_from=ef.created_from,
        created_to=ef.created_to,
    )
    async for groups in iter_pages(
        lambda limit, offset: list_conversation_groups(
            session,
            caller_id=scope.caller.id,
            can_manage=can_manage,
            filters=group_filters,
            order_by="-created_at",
            limit=limit,
            offset=offset,
        )
    ):
        emails = await resolve_user_emails(session, (g.user_id for g in groups))
        scenarios = await resolve_scenario_names(session, (g.scenario_id for g in groups))
        for group in groups:
            yield ConversationGroupRow(
                group=group,
                evaluation_title=evaluation_title,
                owner_email=emails.get(group.user_id),
                scenario_name=scenarios.get(group.scenario_id) if group.scenario_id else None,
            )


_COLUMNS: list[Column[ConversationGroupRow]] = [
    Column("Group ID", lambda r: r.group.id),
    Column("Name", lambda r: r.group.name),
    Column("Evaluation ID", lambda r: r.group.evaluation_id),
    Column("Evaluation title", lambda r: r.evaluation_title),
    Column("Scenario ID", lambda r: r.group.scenario_id),
    Column("Scenario name", lambda r: r.scenario_name),
    Column("Owner ID", lambda r: r.group.user_id),
    Column("Owner email", lambda r: r.owner_email),
    Column("Conversation count", lambda r: len(r.group.conversations)),
    # The member conversation ids, space-separated — so a group joins to the
    # `conversations` / `transcript` exports (whose `Conversation ID` column carries them).
    Column("Conversation IDs", lambda r: " ".join(str(conversation.id) for conversation in r.group.conversations)),
    Column("Created", lambda r: r.group.created_at),
]


CONVERSATION_GROUPS_EXPORT: CsvExport[ConversationGroupRow] = CsvExport(
    key="conversation-groups",
    name="Conversation groups",
    description=(
        "One row per conversation group in the evaluation — its name, evaluation (with title), scenario (with "
        "name), owner (with email), and member conversation ids."
    ),
    permission=Permission.CONVERSATIONS_READ,
    columns=_COLUMNS,
    fetch=_fetch_conversation_groups,
    supported_filters=frozenset({"created_from", "created_to", "scenario_id", "user_id"}),
)
