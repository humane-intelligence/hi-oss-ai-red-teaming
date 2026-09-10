"""The "flags" CSV export — one row per `MessageFlag` in an evaluation.

A self-contained template: its row shape, columns, and (owner/visibility-scoped) fetcher.
The catalog ([catalog.py]) just lists it; nothing here knows about the registry or the endpoint.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.annotations.enums import FlagStatus
from app.core.annotations.filters import MessageFlagFilters
from app.core.annotations.models import MessageFlag
from app.core.annotations.services.message_flags import list_flags
from app.core.auth.roles import Permission
from app.core.config import get_settings
from app.core.csv_generator import Column
from app.core.exports.base import CsvExport
from app.core.exports.base import ExportScope
from app.core.exports.base import iter_pages
from app.core.exports.conversation_messages import message_dicts
from app.core.exports.filters import coerce_status
from app.core.exports.lookups import resolve_scenario_names
from app.core.exports.lookups import resolve_scope_evaluation_title
from app.core.exports.lookups import resolve_task_names
from app.core.exports.lookups import resolve_user_emails
from app.core.restore import restore_cutoff


@dataclass(frozen=True, slots=True)
class FlagRow:
    """A flag plus its resolved labels (evaluation/author/scenario/task) and its flagged-message list."""

    flag: MessageFlag
    evaluation_title: str | None
    author_email: str | None
    scenario_name: str | None
    task_name: str | None
    # Raw flagged-message list (not pre-serialized): the CSV column's `formatter` JSON-encodes it to
    # a string cell, while the JSON export emits it as a real nested array via the column's `value`.
    messages: list[dict[str, str | dict[str, str] | None]]


async def _fetch_flags(session: AsyncSession, scope: ExportScope) -> AsyncIterator[FlagRow]:
    can_manage = scope.full_group_access
    # Every flag shares the scope's single evaluation, so its title resolves once (not per page).
    evaluation_title = await resolve_scope_evaluation_title(session, scope)
    # The flags export honors every export filter: scenario/task/status + author (user_id) + date
    # range, mapped onto the flag's own denormalised columns (`status` coerced to FlagStatus).
    ef = scope.filters
    flag_filters = MessageFlagFilters(
        evaluation_id=scope.evaluation_id,
        scenario_id=ef.scenario_id,
        task_id=ef.task_id,
        created_by_id=ef.user_id,
        created_from=ef.created_from,
        created_to=ef.created_to,
        status=coerce_status(FlagStatus, ef.status),
    )
    # Stream page-by-page; the per-row labels (author/scenario/task) are resolved per page (a
    # bounded batch), not over the whole result set — so peak memory stays ~one page of flags.
    async for flags in iter_pages(
        lambda limit, offset: list_flags(
            session,
            caller_id=scope.caller.id,
            can_manage=can_manage,
            filters=flag_filters,
            order_by="-created_at",
            limit=limit,
            offset=offset,
            deleted_cutoff=restore_cutoff(get_settings()),
        )
    ):
        emails = await resolve_user_emails(session, (flag.created_by_id for flag in flags))
        scenarios = await resolve_scenario_names(session, (flag.scenario_id for flag in flags))
        tasks = await resolve_task_names(session, (flag.task_id for flag in flags))
        for flag in flags:
            yield FlagRow(
                flag=flag,
                evaluation_title=evaluation_title,
                author_email=emails.get(flag.created_by_id),
                scenario_name=scenarios.get(flag.scenario_id) if flag.scenario_id else None,
                task_name=tasks.get(flag.task_id) if flag.task_id else None,
                messages=message_dicts(flag.messages, settings=get_settings()),
            )


_COLUMNS: list[Column[FlagRow]] = [
    Column("Flag ID", lambda r: r.flag.id),
    Column("Status", lambda r: r.flag.status),
    Column("Red-flagged", lambda r: r.flag.red_flagged),
    Column("Reason", lambda r: r.flag.reason),
    Column("Comment", lambda r: r.flag.comment),
    Column("Evaluation ID", lambda r: r.flag.evaluation_id),
    Column("Evaluation title", lambda r: r.evaluation_title),
    Column("Conversation ID", lambda r: r.flag.conversation_id),
    Column("Scenario ID", lambda r: r.flag.scenario_id),
    Column("Scenario name", lambda r: r.scenario_name),
    Column("Task ID", lambda r: r.flag.task_id),
    Column("Task name", lambda r: r.task_name),
    Column("Author ID", lambda r: r.flag.created_by_id),
    Column("Author email", lambda r: r.author_email),
    Column("Flagged message count", lambda r: len(r.flag.messages)),
    # The flagged message ids (and the turns they sit in), space-separated by (created_at, id)
    # — so a flag can be joined back to the `transcript` / `engagement_report` exports (whose
    # `Message ID` / `Turn ID` columns carry the same ids). Intra-turn ties break on the UUID id,
    # not role/slot, so this order is a stable join key, not a per-role ordering.
    Column("Flagged message IDs", lambda r: " ".join(str(message.id) for message in r.flag.messages)),
    Column("Flagged turn IDs", lambda r: " ".join(dict.fromkeys(str(message.turn_id) for message in r.flag.messages))),
    # The full flagged-message payload (id + turn + role + status + content + tags): a real nested array in
    # the JSON export, JSON-encoded to one string cell in CSV — so every selected message's content
    # is exported and each id stays paired with its text.
    Column("Flagged messages", value=lambda r: r.messages, formatter=lambda v: json.dumps(v, ensure_ascii=False)),
    Column("Created", lambda r: r.flag.created_at),
]


FLAGS_EXPORT: CsvExport[FlagRow] = CsvExport(
    key="flags",
    name="Message flags",
    description=(
        "One row per message flag in the evaluation — status, reason, denormalised context (with resolved "
        "scenario/task/author names), the flagged message/turn ids (so a flag joins back to the transcript / "
        "engagement-report exports), and the flagged messages themselves (id + content + tags) as a JSON "
        "cell — the tags as stored, with no policy column: use the transcript export to see which keys "
        "the evaluation keeps out of the prompt."
    ),
    permission=Permission.FLAGS_READ,
    columns=_COLUMNS,
    fetch=_fetch_flags,
    supported_filters=frozenset({"created_from", "created_to", "status", "scenario_id", "task_id", "user_id"}),
)
