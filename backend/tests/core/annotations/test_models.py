"""Integration tests for this module's model persistence: `Note` / `NotedMessage` link rows,
and the `AnnotationLabel` table constraints (the CHECK and the two partial unique indexes).
"""

from datetime import UTC
from datetime import datetime
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.annotations.models import AnnotationLabel
from app.core.annotations.models import Note
from app.core.annotations.models import NotedMessage
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from tests.core.annotations.conftest import persist_conversation_target
from tests.core.annotations.conftest import persist_user

pytestmark = pytest.mark.integration


async def test_note_persists_with_its_message_set(db_session: AsyncSession) -> None:
    """A note carries denormalised ancestry and projects its link rows as `messages`."""
    target = await persist_conversation_target(db_session, message_count=2)

    note = Note(
        text="Refuses on the first ask, complies after the reframing.",
        created_by_id=target.conversation.user_id,
        conversation_id=target.conversation.id,
        evaluation_id=target.evaluation.id,
        evaluation_group_id=target.group.id,
    )
    db_session.add(note)
    await db_session.flush()
    db_session.add_all([NotedMessage(note_id=note.id, message_id=message.id) for message in target.messages])
    await db_session.flush()
    await db_session.refresh(note, attribute_names=["messages", "created_at", "updated_at"])

    assert {message.id for message in note.messages} == {message.id for message in target.messages}
    assert note.created_at is not None
    assert note.updated_at is not None
    assert note.deleted_at is None


async def test_note_messages_load_chronologically(db_session: AsyncSession) -> None:
    """`messages` follows the messages' own `created_at`, not the order the link rows were written."""
    target = await persist_conversation_target(db_session, message_count=2)
    older, newer = target.messages
    # `created_at` defaults to `now()`, which is the *transaction* timestamp — both rows would
    # share it and the `id` tiebreak would decide the order. Stamp them apart explicitly.
    older.created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    newer.created_at = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    await db_session.flush()

    note = Note(
        text="Both turns drift toward the same refusal.",
        created_by_id=target.conversation.user_id,
        conversation_id=target.conversation.id,
        evaluation_id=target.evaluation.id,
        evaluation_group_id=target.group.id,
    )
    db_session.add(note)
    await db_session.flush()
    db_session.add_all(
        [
            NotedMessage(note_id=note.id, message_id=newer.id),
            NotedMessage(note_id=note.id, message_id=older.id),
        ]
    )
    await db_session.flush()
    await db_session.refresh(note, attribute_names=["messages"])

    assert [message.id for message in note.messages] == [older.id, newer.id]


async def test_note_messages_put_a_turns_prompt_before_its_reply(db_session: AsyncSession) -> None:
    """Within one turn `created_at` ties, so the prompt/reply order comes from the role key.

    A turn's messages are written in one transaction and share `now()` to the
    microsecond; ordering on `created_at, id` alone would let the uuid4 tiebreak
    decide, and disagree with the transcript endpoint every other read.
    """
    target = await persist_conversation_target(db_session)
    turn = Turn(conversation_id=target.conversation.id, turn_index=99)
    db_session.add(turn)
    await db_session.flush()
    # Pinned ids, so the `id` tiebreak would sort the reply first: with a random
    # uuid4 pair this test would only fail half the time against a broken order.
    prompt = Message(
        id=UUID(int=2),
        turn_id=turn.id,
        role=MessageRole.USER,
        status=MessageStatus.COMPLETE,
        content="ask",
    )
    reply = Message(
        id=UUID(int=1),
        turn_id=turn.id,
        role=MessageRole.ASSISTANT,
        status=MessageStatus.COMPLETE,
        content="answer",
    )
    db_session.add_all([prompt, reply])
    await db_session.flush()
    await db_session.refresh(prompt)
    await db_session.refresh(reply)
    assert prompt.created_at == reply.created_at, "precondition: the tie this ordering has to survive"

    note = Note(
        text="Complies once the ask is reframed.",
        created_by_id=target.conversation.user_id,
        conversation_id=target.conversation.id,
        evaluation_id=target.evaluation.id,
        evaluation_group_id=target.group.id,
    )
    db_session.add(note)
    await db_session.flush()
    db_session.add_all(
        [
            NotedMessage(note_id=note.id, message_id=reply.id),
            NotedMessage(note_id=note.id, message_id=prompt.id),
        ]
    )
    await db_session.flush()
    await db_session.refresh(note, attribute_names=["messages"])

    assert [message.id for message in note.messages] == [prompt.id, reply.id]


async def test_a_second_live_curated_row_cannot_reuse_a_key(db_session: AsyncSession) -> None:
    """`ix_annotation_labels_key` is what makes the catalog key an identity, not a label."""
    db_session.add(AnnotationLabel(key="jailbreak", name="Jailbreak"))
    await db_session.flush()

    db_session.add(AnnotationLabel(key="jailbreak", name="A rival entry"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_tombstoned_key_can_be_re_added(db_session: AsyncSession) -> None:
    """Partial on live rows: a tombstone leaves the index, so its key can be inserted again."""
    retired = AnnotationLabel(key="jailbreak", name="Jailbreak")
    db_session.add(retired)
    await db_session.flush()
    retired.soft_delete(uuid4())
    await db_session.flush()

    db_session.add(AnnotationLabel(key="jailbreak", name="Jailbreak"))
    await db_session.flush()  # no IntegrityError: the tombstone is out of the index


async def test_one_author_cannot_hold_two_labels_of_the_same_folded_name(db_session: AsyncSession) -> None:
    """`(created_by_id, lower(name))` — the expression index the migration is hand-written for.

    The author must be a real user: a synthetic id trips the FK first, and the test would then
    pass on the wrong `IntegrityError`.
    """
    author = await persist_user(db_session)
    db_session.add(AnnotationLabel(name="Jailbreak", created_by_id=author.id))
    await db_session.flush()

    db_session.add(AnnotationLabel(name="jailbreak", created_by_id=author.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_two_authors_may_each_hold_the_same_name(db_session: AsyncSession) -> None:
    """The pool is per author, so the index must not collapse them."""
    first = await persist_user(db_session)
    second = await persist_user(db_session)
    db_session.add(AnnotationLabel(name="Jailbreak", created_by_id=first.id))
    db_session.add(AnnotationLabel(name="Jailbreak", created_by_id=second.id))

    await db_session.flush()


@pytest.mark.parametrize("with_author", [True, False], ids=["both-a-key-and-an-author", "neither"])
async def test_a_row_must_be_curated_xor_authored(db_session: AsyncSession, with_author: bool) -> None:
    """The CHECK: a hybrid row would slip past both uniqueness rules and belong to neither kind.

    No FK can be blamed for either failure — the `both` half names a real author, the `neither`
    half names none — so only the CHECK is left to reject them.
    """
    author = await persist_user(db_session) if with_author else None
    db_session.add(
        AnnotationLabel(
            key="jailbreak" if with_author else None,
            name="Hybrid",
            created_by_id=author.id if author else None,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()
