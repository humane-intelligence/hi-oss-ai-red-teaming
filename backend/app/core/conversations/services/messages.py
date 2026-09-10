"""Message write-path service — turn/message mutations over an `AsyncSession`.

Pure async functions; transaction boundaries belong to the caller (the endpoint /
A-B orchestrator commits). `open_turn` must run under the conversation row lock
(`get_conversation(..., for_update=True)`) so `turn_index = max + 1` is race-free:
concurrent posts to the same conversation serialise on that lock, and the unique
`(conversation_id, turn_index)` index is the backstop.

A `Conversation` binds a single model, so a turn has exactly one assistant reply;
`slot` stays null until a multi-model conversation shape exists. History is
**linear** — regenerate/continue insert a new placeholder superseding the live
one (`replaces_message_id`), and the superseded row stays as an append-only
artefact. "Live" = the message nothing supersedes.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.chat import ContentPart
from app.core.ai_gateway.chat import ImageContentPart
from app.core.ai_gateway.chat import ImageUrl
from app.core.ai_gateway.chat import TextContentPart
from app.core.config import Settings
from app.core.conversations.content_crypto import seal_content
from app.core.conversations.content_crypto import unseal_content
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import MESSAGE_ROLE_ORDER
from app.core.conversations.models import Conversation
from app.core.conversations.models import Message
from app.core.conversations.models import MessageImage
from app.core.conversations.models import Turn
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.media.services.images import cached_data_url
from app.core.pagination import paginate

logger = get_logger(__name__)


@dataclass(frozen=True)
class OpenedTurn:
    """Result of `open_turn`: the turn and its live messages.

    `replayed` is True when an idempotent retry (same `client_message_id`)
    returned the existing turn instead of creating one — its assistant message may
    then already be finalised. The streaming placeholder to generate into is the
    message in ``messages`` whose status is `streaming`.
    """

    turn: Turn
    messages: list[Message]
    replayed: bool


async def live_turn_messages(session: AsyncSession, turn_id: UUID) -> list[Message]:
    """Return a turn's live messages — those nothing supersedes — user prompt first.

    "Live" excludes any message another supersedes (via `replaces_message_id`). It is
    supersession-only and deliberately does **not** filter `deleted_at` (messages
    aren't soft-deleted here), unlike `BaseModel.live_select`. Ordered by role, then
    `slot`, then `id` — not `created_at`, which is identical across a turn's messages.
    """
    superseded = select(col(Message.replaces_message_id)).where(
        col(Message.turn_id) == turn_id, col(Message.replaces_message_id).is_not(None)
    )
    statement = (
        select(Message)
        .where(col(Message.turn_id) == turn_id, col(Message.id).not_in(superseded))
        .order_by(MESSAGE_ROLE_ORDER, col(Message.slot), col(Message.id))
    )
    return list((await session.execute(statement)).scalars().all())


def _live_conversation_messages(conversation_id: UUID) -> Select[tuple[Message]]:
    """`Select` for a conversation's live messages, oldest turn first.

    The conversation-wide analogue of `live_turn_messages`: same supersession-only
    "live" rule (a message nothing points at via `replaces_message_id`), widened
    from one turn to every turn of the conversation and ordered across turns by
    `turn_index` first, then the intra-turn `role`/`slot`/`id` key. Like
    `live_turn_messages` it does **not** filter `deleted_at` (messages aren't
    soft-deleted). Returned as a `Select` so callers can read it or hand it to `paginate`.
    """
    superseded = (
        select(col(Message.replaces_message_id))
        .join(Turn, col(Message.turn_id) == col(Turn.id))
        .where(col(Turn.conversation_id) == conversation_id, col(Message.replaces_message_id).is_not(None))
    )
    return (
        select(Message)
        .join(Turn, col(Message.turn_id) == col(Turn.id))
        .where(col(Turn.conversation_id) == conversation_id, col(Message.id).not_in(superseded))
        .order_by(col(Turn.turn_index), MESSAGE_ROLE_ORDER, col(Message.slot), col(Message.id))
    )


async def assert_messages_in_conversation(
    session: AsyncSession, conversation_id: UUID, message_ids: Sequence[UUID]
) -> None:
    """Verify every id in ``message_ids`` is a live message of ``conversation_id``.

    Shared by every entity that attaches itself to a caller-supplied message
    selection (message flags, notes): a foreign or unknown id must not become
    attachable by naming someone else's conversation. A superseded-but-live message
    still qualifies — supersession is not deletion.

    Raises:
        NotFoundError: If any id is not a live message of the conversation.
    """
    found = set(
        (
            await session.execute(
                select(col(Message.id))
                .join(Turn, col(Message.turn_id) == col(Turn.id))
                .where(col(Turn.conversation_id) == conversation_id)
                .where(col(Message.id).in_(message_ids))
                .where(col(Message.deleted_at).is_(None))
                .where(col(Turn.deleted_at).is_(None))
            )
        )
        .scalars()
        .all()
    )
    missing = set(message_ids) - found
    if missing:
        raise NotFoundError(
            f"Messages not found in conversation {conversation_id}: {sorted(str(mid) for mid in missing)}."
        )


async def image_keys_for_messages(session: AsyncSession, message_ids: Sequence[UUID]) -> dict[UUID, list[str]]:
    """Return each message's attached image keys in `position` order, one query for the batch.

    Messages with no attachments are absent from the dict — callers default to `[]`.
    """
    if not message_ids:
        return {}
    statement = (
        select(col(MessageImage.message_id), col(MessageImage.image_key))
        .where(col(MessageImage.message_id).in_(message_ids))
        .order_by(col(MessageImage.message_id), col(MessageImage.position))
    )
    keys_by_message: dict[UUID, list[str]] = {}
    for message_id, image_key in (await session.execute(statement)).all():
        keys_by_message.setdefault(message_id, []).append(image_key)
    return keys_by_message


async def conversation_history(
    session: AsyncSession, conversation_id: UUID, *, settings: Settings
) -> list[ChatMessage]:
    """Build the chat context from a conversation's live turns, oldest turn first.

    Per turn, includes the user prompt and any `complete` assistant reply (skips the
    streaming placeholder and empty content), so the just-opened user message is the
    last entry the model sees. `interrupted`/`error` replies are deliberately excluded —
    only a `complete` assistant reply feeds context. A user message's attached images
    are rebuilt into multi-modal content parts (text + inline images, in attachment
    order); an attachment whose blob has since vanished is dropped with a warning, so a
    deleted image degrades that message, not the whole generation. Reuses the same
    live-message query as the history list (`_live_conversation_messages`).
    """
    messages = (await session.execute(_live_conversation_messages(conversation_id))).scalars().all()
    images = await image_keys_for_messages(session, [message.id for message in messages])
    history: list[ChatMessage] = []
    for message in messages:
        image_keys = images.get(message.id, [])
        if not (message.content or image_keys):
            continue
        if message.role is not MessageRole.USER and message.status is not MessageStatus.COMPLETE:
            continue
        content = unseal_content(message.content, encrypted=message.content_encrypted, settings=settings)
        history.append(await _to_chat_message(message, image_keys, content=content))
    return history


async def _to_chat_message(message: Message, image_keys: Sequence[str], *, content: str) -> ChatMessage:
    """Project a stored `Message` into a `ChatMessage`, inlining attached images as content parts.

    An attachment whose blob is gone (uploader delete or the orphan reaper) is skipped
    rather than raised — a missing prior image must not fail the whole generation.
    """
    if not image_keys:
        return ChatMessage(role=message.role.value, content=content)
    parts: list[ContentPart] = []
    if content:
        parts.append(TextContentPart(text=content))
    for image_key in image_keys:
        try:
            data_url = await cached_data_url(image_key)
        except FileNotFoundError:
            logger.warning("conversations.history.image_missing", message_id=str(message.id), image_key=image_key)
            continue
        parts.append(ImageContentPart(image_url=ImageUrl(url=data_url)))
    # Every attachment vanished and there's no text — nothing to send as a content-part list.
    if not parts:
        return ChatMessage(role=message.role.value, content=content)
    return ChatMessage(role=message.role.value, content=parts)


async def list_conversation_messages(
    session: AsyncSession, conversation_id: UUID, *, limit: int, offset: int
) -> tuple[list[Message], int]:
    """Return one page of a conversation's live messages (oldest turn first) and the total.

    Caller-scoping is the route's job (resolve the conversation under the owner /
    visibility rule first); this only reads the message rows of an
    already-authorised conversation.
    """
    return await paginate(session, _live_conversation_messages(conversation_id), limit=limit, offset=offset)


async def live_messages_for_conversations(
    session: AsyncSession, conversation_ids: Sequence[UUID]
) -> list[tuple[UUID, Message]]:
    """Return `(conversation_id, message)` for every live message across the conversations, in one query.

    The batched analogue of `list_conversation_messages` for exports that flatten many
    already-authorised conversations and want the whole set, not a page: collapses the
    per-conversation N+1 (one paginated drain each) into a single query. Same
    supersession-only "live" rule, ordered by conversation, then `turn_index`, then the
    intra-turn `role`/`slot`/`id` key — so a caller can group the rows by conversation in
    one pass. Caller-scoping stays the caller's job: pass only conversation ids already
    resolved under the owner/visibility rule.
    """
    if not conversation_ids:
        return []
    superseded = (
        select(col(Message.replaces_message_id))
        .join(Turn, col(Message.turn_id) == col(Turn.id))
        .where(col(Turn.conversation_id).in_(conversation_ids), col(Message.replaces_message_id).is_not(None))
    )
    statement = (
        select(col(Turn.conversation_id), Message)
        .join(Turn, col(Message.turn_id) == col(Turn.id))
        .where(col(Turn.conversation_id).in_(conversation_ids), col(Message.id).not_in(superseded))
        .order_by(
            col(Turn.conversation_id), col(Turn.turn_index), MESSAGE_ROLE_ORDER, col(Message.slot), col(Message.id)
        )
    )
    return [(conversation_id, message) for conversation_id, message in (await session.execute(statement)).all()]


async def open_turn(
    session: AsyncSession,
    conversation: Conversation,
    *,
    settings: Settings,
    content: str,
    client_message_id: UUID | None = None,
    image_keys: Sequence[str] | None = None,
    tags: dict[str, str] | None = None,
) -> OpenedTurn:
    """Open a turn: the user message (complete) + an assistant placeholder (streaming).

    Call under the conversation row lock (`get_conversation(..., for_update=True)`) so
    `turn_index = max + 1` is race-free. Without the lock two concurrent posts can
    compute the same index; the unique `(conversation_id, turn_index)` index then
    rejects the loser and this raises `ConflictError` (a clean 409, not a 500).

    Idempotent on `client_message_id`, an **authoritative** key: a repeat in the same
    conversation replays the existing turn and **ignores ``content``** (a client that
    edited the text must send a new key); a key already used in a different
    conversation is a `ConflictError`. The index is computed over all turns
    (soft-deleted included), so it never repeats.

    Raises:
        ConflictError: ``client_message_id`` already belongs to another conversation,
            or a concurrent post won the `turn_index` race (caller held no row lock).
    """
    if client_message_id is not None:
        replay = await _replay(session, conversation, client_message_id)
        if replay is not None:
            return replay

    max_index = (
        await session.execute(select(func.max(Turn.turn_index)).where(col(Turn.conversation_id) == conversation.id))
    ).scalar_one()
    turn = Turn(conversation_id=conversation.id, turn_index=0 if max_index is None else max_index + 1)
    session.add(turn)
    # Two flushes (turn before its messages — there's no ORM relationship to order the
    # FK), each guarded: a race without the conversation row lock is rejected by the
    # unique (conversation_id, turn_index) on the turn flush, or by the partial-unique
    # client_message_id on the message flush. Both mean "lost the race" — surface a
    # clean 409 rather than an opaque 500.
    try:
        await session.flush()
    except IntegrityError as exc:
        # Lost the `(conversation_id, turn_index)` race (ran without the row lock).
        raise ConflictError("Concurrent turn creation; retry the request.") from exc

    stored_content, content_encrypted = seal_content(
        content, protected=conversation.content_protected, settings=settings
    )
    user_message = Message(
        turn_id=turn.id,
        role=MessageRole.USER,
        status=MessageStatus.COMPLETE,
        content=stored_content,
        content_encrypted=content_encrypted,
        client_message_id=client_message_id,
        tags=tags or {},
    )
    placeholder = Message(turn_id=turn.id, role=MessageRole.ASSISTANT, status=MessageStatus.STREAMING)
    session.add_all([user_message, placeholder])
    try:
        await session.flush()
    except IntegrityError as exc:
        # The partial-unique `client_message_id` rejected the row. A *deterministic*
        # reuse is already caught up-front by `_replay`; reaching here means two
        # concurrent posts shared the key and this one lost — a retry resolves to the
        # winner's stored reply (replay) or the cross-conversation 409.
        raise ConflictError("Concurrent submission of the same client_message_id; retry the request.") from exc
    if image_keys:
        session.add_all(
            MessageImage(message_id=user_message.id, position=position, image_key=image_key)
            for position, image_key in enumerate(image_keys)
        )
        await session.flush()
    return OpenedTurn(turn=turn, messages=[user_message, placeholder], replayed=False)


async def open_replacement(
    session: AsyncSession, conversation: Conversation, message_id: UUID
) -> tuple[Message, Message]:
    """Open an assistant placeholder superseding ``message_id`` (backs regenerate/continue).

    The new streaming placeholder lives in the same turn and slot and points at the
    superseded message via `replaces_message_id`. A terminal target is left untouched
    (append-only); a still-`streaming` target is first flipped to `interrupted`
    (guarded, so a concurrent finalize that already made it terminal is not
    overwritten) so the turn never holds two `streaming` rows. The target must be a
    live assistant message of the conversation's **last** turn — history is linear, so
    regenerate/continue act only on the most recent exchange; replacing an earlier
    reply would feed the later turns back into the prompt as bogus context.

    Returns:
        ``(superseded, placeholder)`` — the superseded reply (so ``continue`` can read
        its text without a second fetch) and the new streaming placeholder.

    Raises:
        NotFoundError: ``message_id`` is not a message of a turn in ``conversation``.
        BadRequestError: ``message_id`` is not an assistant message.
        ConflictError: ``message_id`` is already superseded (history is linear — a
            message has at most one successor), or it is not in the conversation's
            last turn.
    """
    superseded = await _conversation_message(session, conversation, message_id)
    if superseded.role is not MessageRole.ASSISTANT:
        raise BadRequestError(f"Message {message_id} is not an assistant reply.")
    # (this turn's index, the conversation's max turn index) in one round-trip — equal
    # only when the target sits in the last turn.
    target_index, latest_index = (
        await session.execute(
            select(
                col(Turn.turn_index),
                select(func.max(Turn.turn_index)).where(col(Turn.conversation_id) == conversation.id).scalar_subquery(),
            ).where(col(Turn.id) == superseded.turn_id)
        )
    ).one()
    if target_index != latest_index:
        raise ConflictError(
            f"Message {message_id} is not in the conversation's last turn; "
            "only the latest reply can be regenerated or continued."
        )
    already = (
        await session.execute(select(col(Message.id)).where(col(Message.replaces_message_id) == message_id))
    ).first()
    if already is not None:
        raise ConflictError(f"Message {message_id} has already been superseded.")
    if superseded.status is MessageStatus.STREAMING:
        # Flip a still-streaming target to `interrupted` before adding the replacement,
        # so the turn never holds two `streaming` rows. Guarded (`WHERE
        # status='streaming'`), so a concurrent finalize that already made it terminal
        # is not overwritten.
        await finalize_message(
            session,
            superseded.id,
            content=superseded.content,
            status=MessageStatus.INTERRUPTED,
            encrypted=superseded.content_encrypted,  # already sealed; re-sealing would double-wrap it
        )
    placeholder = Message(
        turn_id=superseded.turn_id,
        role=MessageRole.ASSISTANT,
        status=MessageStatus.STREAMING,
        slot=superseded.slot,
        replaces_message_id=superseded.id,
    )
    session.add(placeholder)
    await session.flush()
    return superseded, placeholder


async def finalize_message(
    session: AsyncSession,
    message_id: UUID,
    *,
    content: str,
    status: MessageStatus,
    encrypted: bool,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Finalise a streaming placeholder in place: set `content` + `status` (+ `extra`).

    Guarded `WHERE status = 'streaming'`, so a double finalize (or finalising an
    already-terminal message) is a no-op. ``extra`` carries per-message generation
    metadata (finish_reason / usage / structured error) and is only written when
    given. Returns whether a row was updated — callers use it to detect the
    lost-race / already-finalised case.

    ``encrypted`` has no default and travels with ``content``: the caller that produced the value is
    the only one that knows which form it is in, and a row whose discriminator disagrees with its
    column is unreadable.
    """
    values: dict[str, Any] = {"content": content, "status": status, "content_encrypted": encrypted}
    if extra is not None:
        values["extra"] = extra
    result = await session.execute(
        update(Message)
        .where(col(Message.id) == message_id, col(Message.status) == MessageStatus.STREAMING)
        .values(**values)
    )
    return result.rowcount > 0  # ty: ignore[unresolved-attribute]  # CursorResult at runtime; execute() is typed as Result


async def _replay(session: AsyncSession, conversation: Conversation, client_message_id: UUID) -> OpenedTurn | None:
    """Return the existing turn for a repeated ``client_message_id``, or None if first-seen.

    The key is authoritative — the resubmitted content is not compared, so a replay
    returns the original turn unchanged.
    """
    existing = (
        await session.execute(select(Message).where(col(Message.client_message_id) == client_message_id))
    ).scalar_one_or_none()
    if existing is None:
        return None
    turn = await session.get(Turn, existing.turn_id)
    if turn is None or turn.conversation_id != conversation.id:
        raise ConflictError(f"client_message_id {client_message_id} already used in another conversation.")
    return OpenedTurn(turn=turn, messages=await live_turn_messages(session, turn.id), replayed=True)


async def _conversation_message(session: AsyncSession, conversation: Conversation, message_id: UUID) -> Message:
    """Fetch ``message_id`` only if it belongs to a turn of ``conversation`` (else 404)."""
    statement = (
        select(Message)
        .join(Turn, col(Message.turn_id) == col(Turn.id))
        .where(col(Message.id) == message_id, col(Turn.conversation_id) == conversation.id)
    )
    message = (await session.execute(statement)).scalar_one_or_none()
    if message is None:
        raise NotFoundError(f"Message {message_id} not found in conversation {conversation.id}.")
    return message
