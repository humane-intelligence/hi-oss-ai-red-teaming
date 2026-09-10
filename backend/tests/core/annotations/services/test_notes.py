"""Integration tests for the note service — create and read paths.

Authoring is gated on the group's **visibility** plus the caller's permission, with no
conversation-ownership predicate: an annotator notes a red-teamer's transcript. That is the
one deliberate divergence from `MessageFlag`, so it carries the first test in this file. Reads, in
contrast, are owner-scoped exactly like flags, with `evaluation_groups:manage` as the break-glass.
"""

from datetime import UTC
from datetime import datetime
from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.annotations.filters import NoteFilters
from app.core.annotations.models import Note
from app.core.annotations.models import NotedMessage
from app.core.annotations.schemas import NoteCreate
from app.core.annotations.schemas import NoteUpdateChanges
from app.core.annotations.services.notes import create_note
from app.core.annotations.services.notes import get_note
from app.core.annotations.services.notes import list_notes
from app.core.annotations.services.notes import soft_delete_note
from app.core.annotations.services.notes import update_note
from app.core.config import get_settings
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Message
from app.core.conversations.models import Turn
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.exceptions import NotFoundError
from app.core.restore import restore_cutoff
from tests.core.annotations.conftest import ConversationTarget
from tests.core.annotations.conftest import messages_for
from tests.core.annotations.conftest import persist_conversation_target
from tests.core.annotations.conftest import persist_user

pytestmark = pytest.mark.integration


async def _link_rows(db_session: AsyncSession, note_id: UUID) -> list[NotedMessage]:
    result = await db_session.execute(select(NotedMessage).where(col(NotedMessage.note_id) == note_id))
    return list(result.scalars().all())


async def test_create_note_on_foreign_conversation_in_visible_group(db_session: AsyncSession) -> None:
    """A caller who does not own the conversation notes it, and is recorded as the author."""
    target = await persist_conversation_target(db_session, message_count=2)
    annotator = await persist_user(db_session)
    assert target.conversation.user_id != annotator.id

    note = await create_note(
        db_session,
        NoteCreate(
            conversation_id=target.conversation.id,
            message_ids=[message.id for message in target.messages],
            text="Refuses on the first ask, complies after the reframing.",
        ),
        caller_id=annotator.id,
    )

    assert note.created_by_id == annotator.id
    assert note.conversation_id == target.conversation.id
    assert note.evaluation_id == target.evaluation.id
    assert note.evaluation_group_id == target.group.id
    expected = {message.id for message in target.messages}
    assert {message.id for message in note.messages} == expected
    assert {link.message_id for link in await _link_rows(db_session, note.id)} == expected


async def test_create_note_rejects_invisible_group(db_session: AsyncSession) -> None:
    """An `invitation_only` group the caller holds no role in reads as missing, not forbidden."""
    target = await persist_conversation_target(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    outsider = await persist_user(db_session)

    with pytest.raises(NotFoundError):
        await create_note(
            db_session,
            NoteCreate(
                conversation_id=target.conversation.id,
                message_ids=[target.messages[0].id],
                text="unreachable",
            ),
            caller_id=outsider.id,
        )


async def test_create_note_rejects_public_draft_group(db_session: AsyncSession) -> None:
    """`public` is gated on `status != draft`, so an incomplete group cannot be noted."""
    target = await persist_conversation_target(db_session, status=PublicationStatus.DRAFT)
    outsider = await persist_user(db_session)

    with pytest.raises(NotFoundError):
        await create_note(
            db_session,
            NoteCreate(
                conversation_id=target.conversation.id,
                message_ids=[target.messages[0].id],
                text="draft group",
            ),
            caller_id=outsider.id,
        )


@pytest.mark.parametrize("dead_parent", ["conversation", "evaluation", "group"])
async def test_create_note_rejects_soft_deleted_parents(db_session: AsyncSession, dead_parent: str) -> None:
    """Liveness of the whole `conversation → evaluation → group` chain is a create precondition."""
    target = await persist_conversation_target(db_session)
    annotator = await persist_user(db_session)
    getattr(target, dead_parent).soft_delete(None)
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await create_note(
            db_session,
            NoteCreate(
                conversation_id=target.conversation.id,
                message_ids=[target.messages[0].id],
                text=f"dead {dead_parent}",
            ),
            caller_id=annotator.id,
        )


async def test_create_note_rejects_foreign_message(db_session: AsyncSession) -> None:
    """A message id belonging to another conversation cannot be noted through this one."""
    target = await persist_conversation_target(db_session)
    other = await persist_conversation_target(db_session)
    annotator = await persist_user(db_session)

    with pytest.raises(NotFoundError):
        await create_note(
            db_session,
            NoteCreate(
                conversation_id=target.conversation.id,
                message_ids=[other.messages[0].id],
                text="foreign message",
            ),
            caller_id=annotator.id,
        )


@pytest.mark.parametrize("dead_row", ["message", "turn"])
async def test_create_note_rejects_soft_deleted_message(db_session: AsyncSession, dead_row: str) -> None:
    """A tombstoned message — or one whose turn is tombstoned — cannot be noted."""
    target = await persist_conversation_target(db_session)
    annotator = await persist_user(db_session)
    message = target.messages[0]
    if dead_row == "message":
        message.soft_delete(None)
    else:
        turn = await db_session.get(Turn, message.turn_id)
        assert turn is not None
        turn.soft_delete(None)
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await create_note(
            db_session,
            NoteCreate(
                conversation_id=target.conversation.id,
                message_ids=[message.id],
                text=f"dead {dead_row}",
            ),
            caller_id=annotator.id,
        )


async def test_create_note_allows_superseded_message(db_session: AsyncSession) -> None:
    """A regenerated-past message stays live, so it keeps carrying the provenance of that output."""
    target = await persist_conversation_target(db_session)
    superseded = target.messages[0]
    (later_message,) = await messages_for(db_session, target.conversation.id, count=1, first_turn_index=1)
    survivor = Message(
        turn_id=later_message.turn_id,
        role=MessageRole.ASSISTANT,
        status=MessageStatus.COMPLETE,
        content="regenerated",
        replaces_message_id=superseded.id,
    )
    db_session.add(survivor)
    await db_session.flush()
    annotator = await persist_user(db_session)

    note = await create_note(
        db_session,
        NoteCreate(
            conversation_id=target.conversation.id,
            message_ids=[superseded.id],
            text="The output that was regenerated away is the interesting one.",
        ),
        caller_id=annotator.id,
    )

    assert [message.id for message in note.messages] == [superseded.id]


async def _write_note(
    db_session: AsyncSession,
    target: ConversationTarget,
    *,
    caller_id: UUID,
    text: str = "note",
    message_ids: list[UUID] | None = None,
) -> Note:
    """Create one note through the real service path."""
    return await create_note(
        db_session,
        NoteCreate(
            conversation_id=target.conversation.id,
            message_ids=message_ids or [target.messages[0].id],
            text=text,
        ),
        caller_id=caller_id,
    )


async def test_get_note_returns_own(db_session: AsyncSession) -> None:
    """The author reads their own note, message selection included."""
    target = await persist_conversation_target(db_session)
    annotator = await persist_user(db_session)
    created = await _write_note(db_session, target, caller_id=annotator.id, text="mine")

    fetched = await get_note(db_session, created.id, caller_id=annotator.id)

    assert fetched.id == created.id
    assert fetched.text == "mine"
    assert [message.id for message in fetched.messages] == [target.messages[0].id]


async def test_get_note_hides_other_users(db_session: AsyncSession) -> None:
    """Another user's note reads as missing, even in a group both can see."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    other = await persist_user(db_session)
    created = await _write_note(db_session, target, caller_id=author.id)

    with pytest.raises(NotFoundError):
        await get_note(db_session, created.id, caller_id=other.id)

    notes, total = await list_notes(
        db_session,
        caller_id=other.id,
        filters=NoteFilters(),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )
    assert notes == []
    assert total == 0


async def test_manage_break_glass_sees_foreign_note(db_session: AsyncSession) -> None:
    """`evaluation_groups:manage` lifts the owner scope on read."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    manager = await persist_user(db_session)
    created = await _write_note(db_session, target, caller_id=author.id)

    fetched = await get_note(db_session, created.id, caller_id=manager.id, can_manage=True)

    assert fetched.id == created.id
    _, total = await list_notes(
        db_session,
        caller_id=manager.id,
        can_manage=True,
        filters=NoteFilters(),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )
    assert total == 1


@pytest.mark.parametrize("dead_parent", ["conversation", "evaluation", "group"])
async def test_soft_deleted_parent_hides_note_even_from_manager(db_session: AsyncSession, dead_parent: str) -> None:
    """Parent liveness is enforced regardless of break-glass — and `total` drops with `items`."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    created = await _write_note(db_session, target, caller_id=author.id)
    getattr(target, dead_parent).soft_delete(None)
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await get_note(db_session, created.id, caller_id=author.id, can_manage=True)

    notes, total = await list_notes(
        db_session,
        caller_id=author.id,
        can_manage=True,
        filters=NoteFilters(),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )
    assert notes == []
    assert total == 0


async def test_losing_group_visibility_hides_note_from_author(db_session: AsyncSession) -> None:
    """An author who can no longer see the group loses their note; a manager still reads it."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    created = await _write_note(db_session, target, caller_id=author.id)
    target.group.access_level = EvaluationGroupAccessLevel.INVITATION_ONLY
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await get_note(db_session, created.id, caller_id=author.id)

    fetched = await get_note(db_session, created.id, caller_id=author.id, can_manage=True)
    assert fetched.id == created.id


async def test_locked_read_overwrites_a_stale_cached_row(db_session: AsyncSession) -> None:
    """`for_update` must hand back the locked DB state, not the values an earlier read cached.

    The out-of-band write uses ``synchronize_session=False`` so the in-memory instance keeps
    the pre-update value — which is exactly the state a concurrent writer would leave behind,
    and what `populate_existing` on the locked read has to discard.
    """
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    created = await _write_note(db_session, target, caller_id=author.id, text="first reading")
    await db_session.execute(
        update(Note)
        .where(col(Note.id) == created.id)
        .values(text="out of band")
        .execution_options(synchronize_session=False)
    )
    assert created.text == "first reading"

    locked = await get_note(db_session, created.id, caller_id=author.id, for_update=True)

    assert locked is created
    assert locked.text == "out of band"


async def test_message_id_filter_matches_a_multi_message_note(db_session: AsyncSession) -> None:
    """Filtering by any member of a multi-message selection returns that note, exactly once."""
    target = await persist_conversation_target(db_session, message_count=3)
    author = await persist_user(db_session)
    created = await _write_note(
        db_session, target, caller_id=author.id, message_ids=[message.id for message in target.messages]
    )

    notes, total = await list_notes(
        db_session,
        caller_id=author.id,
        filters=NoteFilters(message_id=target.messages[1].id),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    assert [note.id for note in notes] == [created.id]
    assert total == 1


async def test_message_id_filter_drops_notes_that_omit_the_message(db_session: AsyncSession) -> None:
    """A note on a different message of the same conversation is excluded."""
    target = await persist_conversation_target(db_session, message_count=3)
    author = await persist_user(db_session)
    wanted = await _write_note(db_session, target, caller_id=author.id, message_ids=[target.messages[0].id])
    await _write_note(db_session, target, caller_id=author.id, message_ids=[target.messages[2].id])

    notes, total = await list_notes(
        db_session,
        caller_id=author.id,
        filters=NoteFilters(message_id=target.messages[0].id),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    assert [note.id for note in notes] == [wanted.id]
    assert total == 1


@pytest.mark.parametrize(
    ("order_by", "expect_newest_first"),
    [("-created_at", True), ("created_at", False), ("-updated_at", True), ("updated_at", False)],
)
async def test_list_orders_by_the_requested_column(
    db_session: AsyncSession, order_by: str, expect_newest_first: bool
) -> None:
    """Every `NoteOrderBy` literal sorts the page — the rows are stamped apart to decide it.

    Rows created in one transaction share `created_at`/`updated_at`, so without the
    explicit stamps the `id` tiebreak would answer and the assertion could not fail.
    """
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    older = await _write_note(db_session, target, caller_id=author.id, text="older")
    newer = await _write_note(db_session, target, caller_id=author.id, text="newer")
    older.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    older.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    newer.created_at = datetime(2026, 2, 1, tzinfo=UTC)
    newer.updated_at = datetime(2026, 2, 1, tzinfo=UTC)
    await db_session.flush()

    notes, _ = await list_notes(
        db_session,
        caller_id=author.id,
        filters=NoteFilters(),
        order_by=order_by,  # ty: ignore[invalid-argument-type]
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    expected = [newer.id, older.id] if expect_newest_first else [older.id, newer.id]
    assert [note.id for note in notes] == expected


@pytest.mark.parametrize("filter_field", ["conversation_id", "evaluation_id", "evaluation_group_id"])
async def test_ancestry_filters_narrow_to_one_parent(db_session: AsyncSession, filter_field: str) -> None:
    """Each ancestry filter drops the caller's own notes that sit under a different parent."""
    kept_target = await persist_conversation_target(db_session)
    other_target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    kept = await _write_note(db_session, kept_target, caller_id=author.id, text="kept")
    await _write_note(db_session, other_target, caller_id=author.id, text="excluded")
    value = {
        "conversation_id": kept_target.conversation.id,
        "evaluation_id": kept_target.evaluation.id,
        "evaluation_group_id": kept_target.group.id,
    }[filter_field]

    notes, total = await list_notes(
        db_session,
        caller_id=author.id,
        filters=NoteFilters.model_validate({filter_field: value}),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    assert [note.id for note in notes] == [kept.id]
    assert total == 1


async def test_created_by_id_filter_narrows_a_break_glass_read(db_session: AsyncSession) -> None:
    """The author filter only does anything once break-glass has lifted the owner scope."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    other_author = await persist_user(db_session)
    manager = await persist_user(db_session)
    mine = await _write_note(db_session, target, caller_id=author.id, text="mine")
    await _write_note(db_session, target, caller_id=other_author.id, text="theirs")

    notes, total = await list_notes(
        db_session,
        caller_id=manager.id,
        can_manage=True,
        filters=NoteFilters(created_by_id=author.id),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    assert [note.id for note in notes] == [mine.id]
    assert total == 1


@pytest.mark.parametrize(("filter_field", "kept_text"), [("created_from", "newer"), ("created_to", "older")])
async def test_created_at_window_keeps_one_side_of_the_boundary(
    db_session: AsyncSession, filter_field: str, kept_text: str
) -> None:
    """`created_from` keeps what is at/after the bound, `created_to` what is at/before it."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    older = await _write_note(db_session, target, caller_id=author.id, text="older")
    newer = await _write_note(db_session, target, caller_id=author.id, text="newer")
    # `created_at` defaults to the transaction timestamp, so both rows land on the same
    # instant and neither bound would be observable without stamping them apart.
    older.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    newer.created_at = datetime(2026, 1, 3, tzinfo=UTC)
    await db_session.flush()

    notes, total = await list_notes(
        db_session,
        caller_id=author.id,
        filters=NoteFilters.model_validate({filter_field: datetime(2026, 1, 2, tzinfo=UTC)}),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    expected = older if kept_text == "older" else newer
    assert [note.id for note in notes] == [expected.id]
    assert total == 1


async def test_list_filters_cannot_widen_scope(db_session: AsyncSession) -> None:
    """A filter naming another user's resource yields an empty page, never a 404 or a leak."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    outsider = await persist_user(db_session)
    await _write_note(db_session, target, caller_id=author.id)

    notes, total = await list_notes(
        db_session,
        caller_id=outsider.id,
        filters=NoteFilters(conversation_id=target.conversation.id, created_by_id=author.id),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    assert notes == []
    assert total == 0


async def test_search_escapes_like_metacharacters(db_session: AsyncSession) -> None:
    """`%` in `search` matches a literal percent sign, not "any characters"."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    literal = await _write_note(db_session, target, caller_id=author.id, text="100% reproducible")
    await _write_note(db_session, target, caller_id=author.id, text="100 out of 100 reproducible")

    notes, total = await list_notes(
        db_session,
        caller_id=author.id,
        filters=NoteFilters(search="100%"),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    assert [note.id for note in notes] == [literal.id]
    assert total == 1


async def test_search_ignores_case(db_session: AsyncSession) -> None:
    """`search` is a case-insensitive substring match, not a case-sensitive one."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    created = await _write_note(db_session, target, caller_id=author.id, text="Refused the Jailbreak framing")

    notes, total = await list_notes(
        db_session,
        caller_id=author.id,
        filters=NoteFilters(search="JAILBREAK"),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    assert [note.id for note in notes] == [created.id]
    assert total == 1


async def test_list_last_page_returns_the_remainder(db_session: AsyncSession) -> None:
    """A partial final page carries the leftover row while `total` still counts them all."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    for index in range(3):
        await _write_note(db_session, target, caller_id=author.id, text=f"note {index}")

    notes, total = await list_notes(
        db_session,
        caller_id=author.id,
        filters=NoteFilters(),
        order_by="-created_at",
        limit=2,
        offset=2,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    assert len(notes) == 1
    assert total == 3


async def test_list_paginates(db_session: AsyncSession) -> None:
    """`total` counts the whole scope while `items` is trimmed to the page window."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    for index in range(3):
        await _write_note(db_session, target, caller_id=author.id, text=f"note {index}")

    notes, total = await list_notes(
        db_session,
        caller_id=author.id,
        filters=NoteFilters(),
        order_by="-created_at",
        limit=2,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    assert len(notes) == 2
    assert total == 3


async def test_list_beyond_last_page_returns_empty_items(db_session: AsyncSession) -> None:
    """An offset past the end is an empty page with the count intact, not an error."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    await _write_note(db_session, target, caller_id=author.id)

    notes, total = await list_notes(
        db_session,
        caller_id=author.id,
        filters=NoteFilters(),
        order_by="-created_at",
        limit=50,
        offset=10,
        deleted_cutoff=restore_cutoff(get_settings()),
    )

    assert notes == []
    assert total == 1


async def test_update_note_writes_only_set_fields(db_session: AsyncSession) -> None:
    """A change carrying `text` rewrites it; the selection and ancestry are untouched."""
    target = await persist_conversation_target(db_session, message_count=2)
    author = await persist_user(db_session)
    created = await _write_note(
        db_session,
        target,
        caller_id=author.id,
        text="first reading",
        message_ids=[message.id for message in target.messages],
    )
    # The locked read is how a mutation path is required to load the row, so the
    # update runs against the same statement shape production uses.
    locked = await get_note(db_session, created.id, caller_id=author.id, for_update=True)

    updated = await update_note(db_session, locked, NoteUpdateChanges(text="revised reading"))

    assert updated.text == "revised reading"
    assert {message.id for message in updated.messages} == {message.id for message in target.messages}
    assert updated.conversation_id == target.conversation.id
    reread = await get_note(db_session, created.id, caller_id=author.id)
    assert reread.text == "revised reading"


async def test_update_note_with_no_fields_is_a_noop(db_session: AsyncSession) -> None:
    """An empty change set leaves the row exactly as it was — nothing is reset to a default."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    created = await _write_note(db_session, target, caller_id=author.id, text="untouched")

    updated = await update_note(db_session, created, NoteUpdateChanges())

    assert updated.text == "untouched"
    assert updated.deleted_at is None


async def test_soft_delete_hides_note(db_session: AsyncSession) -> None:
    """A soft-deleted note stops being readable through the service."""
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    created = await _write_note(db_session, target, caller_id=author.id)

    deleted = await soft_delete_note(db_session, created, by_id=uuid4())

    assert deleted.deleted_at is not None
    with pytest.raises(NotFoundError):
        await get_note(db_session, created.id, caller_id=author.id)
    _, total = await list_notes(
        db_session,
        caller_id=author.id,
        filters=NoteFilters(),
        order_by="-created_at",
        limit=50,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )
    assert total == 0


async def test_soft_delete_leaves_the_loaded_message_set_readable(db_session: AsyncSession) -> None:
    """The delete's refresh must not expire `messages` — a lazy reload raises under asyncio."""
    target = await persist_conversation_target(db_session, message_count=2)
    author = await persist_user(db_session)
    created = await _write_note(
        db_session, target, caller_id=author.id, message_ids=[message.id for message in target.messages]
    )

    deleted = await soft_delete_note(db_session, created, by_id=uuid4())

    # A set: these fixture messages sit in separate turns written by one transaction,
    # so they tie on `created_at` and their relative order is not this test's subject.
    assert {message.id for message in deleted.messages} == {message.id for message in target.messages}


async def test_soft_delete_keeps_link_rows(db_session: AsyncSession) -> None:
    """The link rows survive the soft delete — they are hidden with their parent, not erased."""
    target = await persist_conversation_target(db_session, message_count=2)
    author = await persist_user(db_session)
    created = await _write_note(
        db_session, target, caller_id=author.id, message_ids=[message.id for message in target.messages]
    )

    await soft_delete_note(db_session, created, by_id=uuid4())

    assert {link.message_id for link in await _link_rows(db_session, created.id)} == {
        message.id for message in target.messages
    }
