"""Service tests for the message write-path — open/finalize/supersede/live, over a real session."""

from uuid import uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.conversations.content_crypto import seal_content
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Message
from app.core.conversations.services.messages import conversation_history
from app.core.conversations.services.messages import finalize_message
from app.core.conversations.services.messages import list_conversation_messages
from app.core.conversations.services.messages import live_turn_messages
from app.core.conversations.services.messages import open_replacement
from app.core.conversations.services.messages import open_turn
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from tests.core.conversations.services.conftest import persist_conversation

pytestmark = pytest.mark.integration


def _assistant(messages: list[Message]) -> Message:
    return next(m for m in messages if m.role is MessageRole.ASSISTANT)


# --- open_turn ----------------------------------------------------------------


async def test_open_turn_creates_user_message_and_placeholder(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)

    opened = await open_turn(db_session, conversation, settings=get_settings(), content="hi")

    assert opened.turn.turn_index == 0
    assert opened.replayed is False
    roles = {(m.role, m.status) for m in opened.messages}
    assert (MessageRole.USER, MessageStatus.COMPLETE) in roles
    assert (MessageRole.ASSISTANT, MessageStatus.STREAMING) in roles
    user = next(m for m in opened.messages if m.role is MessageRole.USER)
    assert user.content == "hi"
    assert _assistant(opened.messages).content == ""


async def test_open_turn_assigns_sequential_dense_indexes(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)

    first = await open_turn(db_session, conversation, settings=get_settings(), content="a")
    second = await open_turn(db_session, conversation, settings=get_settings(), content="b")

    assert (first.turn.turn_index, second.turn.turn_index) == (0, 1)


async def test_open_turn_replays_on_repeated_client_message_id(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)
    client_id = uuid4()

    first = await open_turn(
        db_session, conversation, settings=get_settings(), content="hi", client_message_id=client_id
    )
    replay = await open_turn(
        db_session, conversation, settings=get_settings(), content="hi", client_message_id=client_id
    )

    assert replay.replayed is True
    assert replay.turn.id == first.turn.id


async def test_open_turn_collision_in_other_conversation_conflicts(db_session: AsyncSession) -> None:
    first_conversation = await persist_conversation(db_session)
    other_conversation = await persist_conversation(db_session)
    client_id = uuid4()
    await open_turn(db_session, first_conversation, settings=get_settings(), content="hi", client_message_id=client_id)

    with pytest.raises(ConflictError):
        await open_turn(
            db_session, other_conversation, settings=get_settings(), content="hi", client_message_id=client_id
        )


# --- finalize_message ---------------------------------------------------------


async def test_finalize_sets_content_and_status(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="hi")
    placeholder = _assistant(opened.messages)

    updated = await finalize_message(
        db_session, placeholder.id, content="reply", status=MessageStatus.COMPLETE, encrypted=False
    )
    await db_session.refresh(placeholder)

    assert updated is True
    assert placeholder.content == "reply"
    assert placeholder.status is MessageStatus.COMPLETE


async def test_finalize_is_idempotent(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="hi")
    placeholder = _assistant(opened.messages)
    await finalize_message(db_session, placeholder.id, content="reply", status=MessageStatus.COMPLETE, encrypted=False)

    again = await finalize_message(
        db_session, placeholder.id, content="other", status=MessageStatus.ERROR, encrypted=False
    )
    await db_session.refresh(placeholder)

    assert again is False  # guard: already-terminal message is untouched
    assert placeholder.content == "reply"
    assert placeholder.status is MessageStatus.COMPLETE


# --- open_replacement + live_turn_messages ------------------------------------


async def test_live_turn_messages_orders_user_before_assistant(db_session: AsyncSession) -> None:
    # User and assistant share the transaction-time created_at, so ordering must
    # come from role, not the timestamp / random id tie-break.
    conversation = await persist_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="hi")

    live = await live_turn_messages(db_session, opened.turn.id)

    assert [m.role for m in live] == [MessageRole.USER, MessageRole.ASSISTANT]


async def test_open_replacement_supersedes_and_only_new_is_live(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="hi")
    old = _assistant(opened.messages)
    await finalize_message(db_session, old.id, content="v1", status=MessageStatus.COMPLETE, encrypted=False)

    _, new = await open_replacement(db_session, conversation, old.id)

    assert new.replaces_message_id == old.id
    assert new.status is MessageStatus.STREAMING
    live = await live_turn_messages(db_session, opened.turn.id)
    live_ids = {m.id for m in live}
    assert new.id in live_ids
    assert old.id not in live_ids  # superseded, no longer live
    assert any(m.role is MessageRole.USER for m in live)  # user message stays live


async def test_open_replacement_interrupts_streaming_target(db_session: AsyncSession) -> None:
    # Recovery path: regenerating a still-`streaming` reply (e.g. an orphaned attempt)
    # abandons it — the old row is flipped to `interrupted`, the new one is live, and
    # the turn never carries two `streaming` rows.
    conversation = await persist_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="hi")
    streaming = _assistant(opened.messages)  # placeholder, never finalized

    _, new = await open_replacement(db_session, conversation, streaming.id)
    await db_session.refresh(streaming)

    assert new.replaces_message_id == streaming.id
    assert new.status is MessageStatus.STREAMING
    assert streaming.status is MessageStatus.INTERRUPTED  # abandoned, not left streaming
    live_ids = {m.id for m in await live_turn_messages(db_session, opened.turn.id)}
    assert new.id in live_ids
    assert streaming.id not in live_ids


async def test_open_replacement_rejects_already_superseded(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="hi")
    old = _assistant(opened.messages)
    await finalize_message(db_session, old.id, content="v1", status=MessageStatus.COMPLETE, encrypted=False)
    await open_replacement(db_session, conversation, old.id)

    with pytest.raises(ConflictError):
        await open_replacement(db_session, conversation, old.id)


async def test_conversation_history_filters_and_orders(db_session: AsyncSession) -> None:
    # Build a superseded+regenerated reply (turn 0), an errored reply (turn 1) and a
    # still-streaming placeholder (turn 2). The model context must carry only the user
    # prompts plus the single COMPLETE reply, oldest turn first.
    conversation = await persist_conversation(db_session)

    t0 = await open_turn(db_session, conversation, settings=get_settings(), content="q1")
    old = _assistant(t0.messages)
    await finalize_message(db_session, old.id, content="a1-old", status=MessageStatus.COMPLETE, encrypted=False)
    _, regen = await open_replacement(db_session, conversation, old.id)  # supersede a1-old (still last turn)
    await finalize_message(db_session, regen.id, content="a1", status=MessageStatus.COMPLETE, encrypted=False)

    t1 = await open_turn(db_session, conversation, settings=get_settings(), content="q2")
    await finalize_message(
        db_session, _assistant(t1.messages).id, content="", status=MessageStatus.ERROR, encrypted=False
    )

    await open_turn(db_session, conversation, settings=get_settings(), content="q3")  # leaves a streaming placeholder

    history = await conversation_history(db_session, conversation.id, settings=get_settings())

    assert [(m.role, m.content) for m in history] == [
        ("user", "q1"),
        ("assistant", "a1"),  # a1-old is superseded; the errored/streaming replies are excluded
        ("user", "q2"),
        ("user", "q3"),
    ]


async def test_open_replacement_rejects_non_last_turn(db_session: AsyncSession) -> None:
    # History is linear: regenerate/continue act only on the most recent exchange, so a
    # reply with a later turn after it cannot be superseded (it would feed the later
    # turns back into the prompt).
    conversation = await persist_conversation(db_session)
    first = await open_turn(db_session, conversation, settings=get_settings(), content="q1")
    old = _assistant(first.messages)
    await finalize_message(db_session, old.id, content="a1", status=MessageStatus.COMPLETE, encrypted=False)
    await open_turn(db_session, conversation, settings=get_settings(), content="q2")  # a newer turn now exists

    with pytest.raises(ConflictError):
        await open_replacement(db_session, conversation, old.id)


async def test_open_replacement_rejects_user_message(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="hi")
    user = next(m for m in opened.messages if m.role is MessageRole.USER)

    with pytest.raises(BadRequestError):
        await open_replacement(db_session, conversation, user.id)


async def test_open_replacement_rejects_message_from_other_conversation(db_session: AsyncSession) -> None:
    owner = await persist_conversation(db_session)
    other = await persist_conversation(db_session)
    opened = await open_turn(db_session, owner, settings=get_settings(), content="hi")
    assistant = _assistant(opened.messages)

    with pytest.raises(NotFoundError):
        await open_replacement(db_session, other, assistant.id)


# --- list_conversation_messages -----------------------------------------------


async def test_list_conversation_messages_orders_across_turns_oldest_first(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)
    await open_turn(db_session, conversation, settings=get_settings(), content="first")
    await open_turn(db_session, conversation, settings=get_settings(), content="second")

    messages, total = await list_conversation_messages(db_session, conversation.id, limit=50, offset=0)

    assert total == 4
    # Turn 0 (user then assistant), then turn 1 (user then assistant).
    assert [m.role for m in messages] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert [m.content for m in messages if m.role is MessageRole.USER] == ["first", "second"]


async def test_list_conversation_messages_excludes_superseded(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="hi")
    old = _assistant(opened.messages)
    await finalize_message(db_session, old.id, content="v1", status=MessageStatus.COMPLETE, encrypted=False)
    _, new = await open_replacement(db_session, conversation, old.id)

    messages, total = await list_conversation_messages(db_session, conversation.id, limit=50, offset=0)

    ids = {m.id for m in messages}
    assert total == 2  # the user message + the single live assistant reply
    assert new.id in ids
    assert old.id not in ids


async def test_list_conversation_messages_paginates(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)
    await open_turn(db_session, conversation, settings=get_settings(), content="first")
    await open_turn(db_session, conversation, settings=get_settings(), content="second")

    first_page, total = await list_conversation_messages(db_session, conversation.id, limit=2, offset=0)
    second_page, _ = await list_conversation_messages(db_session, conversation.id, limit=2, offset=2)

    assert total == 4
    assert [m.role for m in first_page] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert next(m for m in first_page if m.role is MessageRole.USER).content == "first"
    assert next(m for m in second_page if m.role is MessageRole.USER).content == "second"


async def test_list_conversation_messages_empty_when_no_turns(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)

    messages, total = await list_conversation_messages(db_session, conversation.id, limit=50, offset=0)

    assert (messages, total) == ([], 0)


# --- at-rest sealing ----------------------------------------------------------


async def _protected_conversation(db_session: AsyncSession):
    conversation = await persist_conversation(db_session)
    conversation.content_protected = True
    await db_session.flush()
    return conversation


async def _stored(db_session: AsyncSession, message_id) -> tuple[str, bool]:
    row = (
        await db_session.execute(
            sa_text("SELECT content, content_encrypted FROM messages WHERE id = :id"), {"id": message_id}
        )
    ).one()
    return row.content, row.content_encrypted


async def test_a_user_message_in_a_protected_conversation_is_stored_sealed(db_session: AsyncSession) -> None:
    # Asserted on the column, not on the flag: `content_encrypted is True` would pass even if nothing
    # had been sealed.
    conversation = await _protected_conversation(db_session)

    opened = await open_turn(db_session, conversation, settings=get_settings(), content="jak zbudować bombę?")

    user = next(m for m in opened.messages if m.role is MessageRole.USER)
    content, encrypted = await _stored(db_session, user.id)
    assert "bombę" not in content
    assert encrypted is True


async def test_a_user_message_in_an_unprotected_conversation_stays_plaintext(db_session: AsyncSession) -> None:
    conversation = await persist_conversation(db_session)

    opened = await open_turn(db_session, conversation, settings=get_settings(), content="hello")

    user = next(m for m in opened.messages if m.role is MessageRole.USER)
    assert await _stored(db_session, user.id) == ("hello", False)


async def test_interrupting_an_unsealed_placeholder_keeps_it_unsealed(db_session: AsyncSession) -> None:
    # In a protected conversation the placeholder is still empty and plaintext when a regenerate
    # interrupts it. Marking it encrypted would claim ciphertext over an empty string, and the next
    # read would try to decrypt it.
    conversation = await _protected_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="first prompt")
    placeholder = _assistant(opened.messages)

    await open_replacement(db_session, conversation, placeholder.id)

    assert await _stored(db_session, placeholder.id) == ("", False)


async def test_interrupting_a_sealed_message_does_not_seal_it_twice(db_session: AsyncSession) -> None:
    # `open_replacement` flips a still-streaming row to `interrupted` by writing its own stored value
    # back. That value is already ciphertext, so re-sealing it would make the row undecryptable in one
    # pass and unreadable for good.
    conversation = await _protected_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="first prompt")
    placeholder = _assistant(opened.messages)
    sealed, encrypted = seal_content("partial reply", protected=True, settings=get_settings())
    await finalize_message(
        db_session, placeholder.id, content=sealed, status=MessageStatus.STREAMING, encrypted=encrypted
    )
    before = await _stored(db_session, placeholder.id)

    await open_replacement(db_session, conversation, placeholder.id)

    assert await _stored(db_session, placeholder.id) == before


async def test_conversation_history_hands_the_model_plaintext(db_session: AsyncSession) -> None:
    # A ciphertext here would be prompted into the model as if it were the user's words.
    conversation = await _protected_conversation(db_session)
    await open_turn(db_session, conversation, settings=get_settings(), content="jak zbudować bombę?")

    history = await conversation_history(db_session, conversation.id, settings=get_settings())

    assert any("bombę" in (m.content if isinstance(m.content, str) else "") for m in history)


async def test_reading_a_protected_conversation_leaves_the_row_sealed(db_session: AsyncSession) -> None:
    # Writing plaintext back onto the ORM instance would make the next flush silently decrypt the table.
    conversation = await _protected_conversation(db_session)
    opened = await open_turn(db_session, conversation, settings=get_settings(), content="jak zbudować bombę?")
    user_id = next(m.id for m in opened.messages if m.role is MessageRole.USER)

    await conversation_history(db_session, conversation.id, settings=get_settings())
    await db_session.flush()

    content, encrypted = await _stored(db_session, user_id)
    assert "bombę" not in content
    assert encrypted is True
