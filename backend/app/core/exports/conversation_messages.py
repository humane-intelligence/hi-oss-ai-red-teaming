"""Shared conversation+message building blocks for the message-carrying export templates.

Two things the templates would otherwise duplicate (and drift on):
- `iter_scoped_conversations` — every conversation of the scoped evaluation the caller may see,
  plus those conversations' live messages grouped per conversation, streamed page-by-page with an
  N+1-avoiding batch. Used by `transcript` and `engagement_report`.
- `message_dicts` — the one embedded-message shape (`message_id` / `turn_id` / `role` / `status` /
  `content` / `tags`) as a list of dicts, so the message set `flags` and `engagement_report` embed (a
  real nested array in the JSON export, a JSON-string cell in CSV) can't drift.
"""

from collections.abc import AsyncIterator
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.config import get_settings
from app.core.conversations.content_crypto import ContentDecryptError
from app.core.conversations.content_crypto import unseal_content
from app.core.conversations.filters import ConversationFilters
from app.core.conversations.models import Conversation
from app.core.conversations.models import Message
from app.core.conversations.services.conversations import list_conversations
from app.core.conversations.services.messages import live_messages_for_conversations
from app.core.exports.base import ExportScope
from app.core.exports.base import iter_pages
from app.core.logging import get_logger
from app.core.restore import restore_cutoff

logger = get_logger(__name__)


def unseal_for_export(message: Message, *, settings: Settings) -> str:
    """Plaintext for one exported row, naming the row when it cannot be opened.

    The failure stays fatal — a partial extract that looks complete is the worse outcome — but the
    job's stored error carries no id, so without this an operator learns only that the whole group's
    export failed.
    """
    try:
        return unseal_content(message.content, encrypted=message.content_encrypted, settings=settings)
    except ContentDecryptError:
        logger.error("exports.message.unseal_failed", message_id=str(message.id))
        raise


def message_dicts(messages: Iterable[Message], *, settings: Settings) -> list[dict[str, str | dict[str, str] | None]]:
    """The shared embedded-message shape for the message-carrying exports.

    A list of dicts (one per message). Templates embed it as their message column's raw `value`
    (a real nested array in the JSON export) and JSON-encode it via a CSV-only `formatter`, so
    `flags` and `engagement_report` can't drift on the key set. `status` distinguishes a genuine
    empty reply from an interrupted/streaming placeholder; `tags` is the per-message context stored
    with that prompt — sent to the model minus any key the evaluation's tagging policy no longer
    allows, so it is what was authored, not necessarily what was received.

    `settings` opens a sealed row. The plaintext lands in the returned dict only — assigning it back
    onto `message.content` would make the next flush rewrite the table in the clear.
    """
    return [
        {
            "message_id": str(message.id),
            "turn_id": str(message.turn_id),
            "role": message.role.value,
            "status": message.status.value,
            "content": unseal_for_export(message, settings=settings),
            "tags": dict(message.tags),
        }
        for message in messages
    ]


# Cap conversation ids per `live_messages_for_conversations` call: it binds them into two
# IN clauses (the main filter + the `superseded` subquery), so ~2x this many parameters
# per statement — kept well under the driver's bind-parameter ceiling. A page (500) is far
# under this, so in practice one page = one message query.
_MESSAGE_FETCH_CHUNK = 10_000


async def iter_scoped_conversations(
    session: AsyncSession, scope: ExportScope
) -> AsyncIterator[tuple[list[Conversation], dict[UUID, list[Message]]]]:
    """Yield the evaluation's caller-visible conversations + their live messages, one page at a time.

    Streams so peak memory is one page of conversations and their messages, not the whole
    evaluation. Per page: an owner/visibility-scoped `list_conversations` page (ordered by
    `created_at`), then batched `live_messages_for_conversations` calls grouped into a
    `conversation_id -> [messages]` map for that page — avoiding an N+1 message drain per
    conversation. The batched query orders rows by conversation, so per-conversation lists
    keep their live/turn order; callers iterate the page's `conversations` (in `created_at`
    order) to emit deterministically.
    """
    can_manage = scope.full_group_access
    # Conversation-level export filters flow through here (so transcript + engagement_report honor
    # scenario / user / date-range); message-level dimensions (status, task) don't apply.
    # NOTE: the date range binds to the *conversation's* created_at, not each message's — so it
    # selects whole conversations started in range (with their full transcript), not messages
    # authored in range. Correct grain for engagement_report (one row per conversation); for the
    # per-message transcript it's a coarser filter, by design (there is no per-message date filter).
    ef = scope.filters
    conversation_filters = ConversationFilters(
        evaluation_id=scope.evaluation_id,
        scenario_id=ef.scenario_id,
        user_id=ef.user_id,
        created_from=ef.created_from,
        created_to=ef.created_to,
    )
    async for conversations in iter_pages(
        lambda limit, offset: list_conversations(
            session,
            caller_id=scope.caller.id,
            can_manage=can_manage,
            filters=conversation_filters,
            order_by="created_at",
            limit=limit,
            offset=offset,
            deleted_cutoff=restore_cutoff(get_settings()),
        )
    ):
        conversation_ids = [conversation.id for conversation in conversations]
        messages_by_conversation: dict[UUID, list[Message]] = {}
        for start in range(0, len(conversation_ids), _MESSAGE_FETCH_CHUNK):
            chunk = conversation_ids[start : start + _MESSAGE_FETCH_CHUNK]
            for conversation_id, message in await live_messages_for_conversations(session, chunk):
                messages_by_conversation.setdefault(conversation_id, []).append(message)
        yield conversations, messages_by_conversation
