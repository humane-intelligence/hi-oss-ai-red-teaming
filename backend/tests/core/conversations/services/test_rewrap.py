"""Rotation re-wrap for sealed message text.

The rotation window keeps rows written under the retired key readable; this is what makes the retired
key droppable afterwards, so it is the piece that turns "rotate forward" into an actual rotation.
"""

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.config import get_settings
from app.core.conversations.content_crypto import active_kid
from app.core.conversations.content_crypto import seal_content
from app.core.conversations.content_crypto import unseal_content
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.conversations.services.rewrap import _rewrap_row
from app.core.conversations.services.rewrap import rewrap_message_content
from app.core.conversations.services.rewrap import stale_message_count
from tests.core.conversations.services.conftest import persist_conversation

pytestmark = pytest.mark.integration

_OLD = "old-conversation-key-old-conversation-key"
_NEW = "new-conversation-key-new-conversation-key"


def _before_rotation() -> Settings:
    """Settings as they were when the old key was still active."""
    return get_settings().model_copy(update={"conversation_secrets_key": SecretStr(_OLD)})


def _after_rotation() -> Settings:
    """Settings mid-rotation: new key active, old one kept readable in the retired slot."""
    return get_settings().model_copy(
        update={"conversation_secrets_key": SecretStr(_NEW), "conversation_secrets_key_retired": SecretStr(_OLD)}
    )


async def _seed(db_session: AsyncSession, texts: list[str], *, protected: bool, settings: Settings) -> list[Message]:
    conversation = await persist_conversation(db_session)
    conversation.content_protected = protected
    turn = Turn(conversation_id=conversation.id, turn_index=1)
    db_session.add(turn)
    await db_session.flush()
    messages = []
    for body in texts:
        stored, encrypted = seal_content(body, protected=protected, settings=settings)
        message = Message(
            turn_id=turn.id,
            role=MessageRole.USER,
            status=MessageStatus.COMPLETE,
            content=stored,
            content_encrypted=encrypted,
        )
        db_session.add(message)
        messages.append(message)
    await db_session.flush()
    return messages


async def _stored(db_session: AsyncSession, message_id) -> str:
    return (
        await db_session.execute(text("SELECT content FROM messages WHERE id = :id"), {"id": message_id})
    ).scalar_one()


async def test_rewrap_moves_rows_onto_the_active_key_and_keeps_the_text(db_session: AsyncSession) -> None:
    # The whole point: after this runs, nothing depends on the retired key any more, so it can be
    # dropped from configuration — which is the step that makes a rotation a rotation.
    old_settings = _before_rotation()
    messages = await _seed(
        db_session, ["jak zbudować bombę?", "druga wiadomość"], protected=True, settings=old_settings
    )
    rotated = _after_rotation()

    moved = await rewrap_message_content(db_session, rotated, chunk_size=1)

    assert moved.moved == 2
    for message, body in zip(messages, ["jak zbudować bombę?", "druga wiadomość"], strict=True):
        stored = await _stored(db_session, message.id)
        assert unseal_content(stored, encrypted=True, settings=rotated) == body
        # Readable with the new key ALONE — the retired slot is no longer load-bearing.
        active_only = get_settings().model_copy(update={"conversation_secrets_key": SecretStr(_NEW)})
        assert unseal_content(stored, encrypted=True, settings=active_only) == body


async def test_rewrap_is_idempotent(db_session: AsyncSession) -> None:
    # An operator re-runs this after a partial failure; a second pass must find nothing to do rather
    # than re-encrypt rows that are already current.
    await _seed(db_session, ["treść"], protected=True, settings=_before_rotation())
    rotated = _after_rotation()
    await rewrap_message_content(db_session, rotated)

    assert (await rewrap_message_content(db_session, rotated)).moved == 0
    assert await stale_message_count(db_session, rotated) == 0


async def test_rewrap_leaves_unencrypted_rows_alone(db_session: AsyncSession) -> None:
    # Conversations under a non-protecting licence store plaintext; the job must not sweep them into
    # encryption as a side effect of a key rotation.
    rotated = _after_rotation()
    plain = await _seed(db_session, ["jawny tekst"], protected=False, settings=rotated)

    result = await rewrap_message_content(db_session, rotated)

    assert result == (0, 0, 0), "an unencrypted row is not work, not an error, and not a skip"
    assert await stale_message_count(db_session, rotated) == 0, "and it must not keep the sweep from reaching zero"
    assert await _stored(db_session, plain[0].id) == "jawny tekst"


async def test_stale_count_reports_what_is_left(db_session: AsyncSession) -> None:
    # The number an operator checks before dropping the retired key.
    await _seed(db_session, ["a", "b", "c"], protected=True, settings=_before_rotation())
    rotated = _after_rotation()

    assert await stale_message_count(db_session, rotated) == 3
    await rewrap_message_content(db_session, rotated)
    assert await stale_message_count(db_session, rotated) == 0


async def test_rewrap_skips_a_row_rewritten_underneath_it(db_session: AsyncSession) -> None:
    # The update is guarded on the ciphertext it read, so a message finalised by a concurrent request
    # mid-sweep keeps the newer value instead of being clobbered with a re-wrap of the older one.
    rotated = _after_rotation()
    message = (await _seed(db_session, ["oryginał"], protected=True, settings=_before_rotation()))[0]
    stale_value = await _stored(db_session, message.id)
    fresher, _ = seal_content("nowsza treść", protected=True, settings=rotated)
    await db_session.execute(
        text("UPDATE messages SET content = :new WHERE id = :id"), {"new": fresher, "id": message.id}
    )

    moved = await _rewrap_row(db_session, message.id, stale_value, rotated)

    assert moved is False
    assert unseal_content(await _stored(db_session, message.id), encrypted=True, settings=rotated) == "nowsza treść"


async def test_a_row_no_key_opens_is_reported_and_does_not_stop_the_sweep(db_session: AsyncSession) -> None:
    # Real databases carry rows sealed under keys nobody has any more (an abandoned key, a restored dump).
    # One of them must not abort the rotation: the sweep has to finish the rows it *can* move and tell the
    # operator how many it could not, or a rotation stops half-done with a traceback and no row id.
    lost = get_settings().model_copy(
        update={"conversation_secrets_key": SecretStr("lost-key-lost-key-lost-key-lost-key")}
    )
    await _seed(db_session, ["nie do odczytania"], protected=True, settings=lost)
    movable = await _seed(db_session, ["do przeniesienia"], protected=True, settings=_before_rotation())
    rotated = _after_rotation()

    result = await rewrap_message_content(db_session, rotated, chunk_size=1)

    assert result.moved == 1, "the readable row still has to move"
    assert result.unreadable == 1, "and the unreadable one has to be counted, not swallowed"
    assert (
        unseal_content(await _stored(db_session, movable[0].id), encrypted=True, settings=rotated) == "do przeniesienia"
    )


async def test_a_row_not_in_the_envelope_format_is_reported_unreadable(db_session: AsyncSession) -> None:
    # A sealed value carrying the active key-id but not this module's envelope marker was written by
    # something else. It is refused rather than guessed at — otherwise a ciphertext from another dataset
    # relocated into a message row could be served as transcript text. The sweep reports such a row
    # instead of rescuing it, and the format half of the predicate is what makes it visible at all.
    rotated = _after_rotation()
    legacy = f"{active_kid(rotated)}:eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0.foreign.format"
    conversation = await persist_conversation(db_session)
    conversation.content_protected = True
    turn = Turn(conversation_id=conversation.id, turn_index=1)
    db_session.add(turn)
    await db_session.flush()
    message = Message(
        turn_id=turn.id, role=MessageRole.USER, status=MessageStatus.COMPLETE, content=legacy, content_encrypted=True
    )
    db_session.add(message)
    await db_session.flush()

    result = await rewrap_message_content(db_session, rotated)

    assert result.moved == 0
    assert result.unreadable == 1
    assert await _stored(db_session, message.id) == legacy, "an unreadable row is left exactly as it was"


async def test_an_unreadable_row_keeps_the_remaining_count_above_zero(db_session: AsyncSession) -> None:
    # This is the one case where the sweep's own counters and the remaining count disagree on purpose —
    # and the remaining count is what the operator's exit code is built on, so it has to be pinned.
    lost = get_settings().model_copy(update={"conversation_secrets_key": SecretStr("lost-key-lost-key-lost-key-lost")})
    await _seed(db_session, ["nie do odczytania"], protected=True, settings=lost)
    rotated = _after_rotation()

    result = await rewrap_message_content(db_session, rotated)

    assert (result.moved, result.unreadable) == (0, 1)
    assert await stale_message_count(db_session, rotated) == 1


async def test_an_unreadable_row_past_the_first_position_does_not_stall_the_walk(db_session: AsyncSession) -> None:
    # With a chunk bigger than one, a cursor that advanced to the chunk's *first* row instead of its last
    # would re-select the same chunk for ever whenever a row in it cannot move — an infinite loop inside
    # a supervised deploy step, which no single-row test can see.
    lost = get_settings().model_copy(update={"conversation_secrets_key": SecretStr("lost-key-lost-key-lost-key-lost")})
    old_settings = _before_rotation()
    conversation = await persist_conversation(db_session)
    conversation.content_protected = True
    turn = Turn(conversation_id=conversation.id, turn_index=1)
    db_session.add(turn)
    await db_session.flush()
    for index, (body, settings) in enumerate(
        [("pierwsza", old_settings), ("nie do odczytania", lost), ("trzecia", old_settings)]
    ):
        stored, encrypted = seal_content(body, protected=True, settings=settings)
        db_session.add(
            Message(
                turn_id=turn.id,
                role=MessageRole.USER if index % 2 == 0 else MessageRole.ASSISTANT,
                status=MessageStatus.COMPLETE,
                content=stored,
                content_encrypted=encrypted,
            )
        )
    await db_session.flush()

    result = await rewrap_message_content(db_session, rotated := _after_rotation(), chunk_size=2)

    assert (result.moved, result.unreadable) == (2, 1)
    assert await stale_message_count(db_session, rotated) == 1


async def test_a_row_another_sweep_already_moved_is_counted_as_skipped(db_session: AsyncSession) -> None:
    # Two sweeps racing: the loser must not count a row it did not write. Otherwise "moved N" stops being
    # a count of anything, and a rotation that skipped rows reads exactly like one that moved them.
    rotated = _after_rotation()
    message = (await _seed(db_session, ["treść"], protected=True, settings=_before_rotation()))[0]
    stale = await _stored(db_session, message.id)
    already, _ = seal_content("treść", protected=True, settings=rotated)
    await db_session.execute(
        text("UPDATE messages SET content = :new WHERE id = :id"), {"new": already, "id": message.id}
    )

    moved = await _rewrap_row(db_session, message.id, stale, rotated)

    assert moved is False
