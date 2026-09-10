"""Integration tests for the annotation service — the shared-read, author-write entity.

The two spines under test: authoring needs group **visibility only** (an annotator labels
a red-teamer's message), and reads are deliberately **not** author-scoped — any reader of
the group sees every annotation in it, which is what makes a tag aggregable. Deletes stay
author-scoped, refused with 403 rather than 404, since the row's existence is already
public to the caller.
"""

from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.annotations.filters import AnnotationFilters
from app.core.annotations.label_catalog import CURATED_ANNOTATION_LABELS
from app.core.annotations.label_catalog import curated_label_id
from app.core.annotations.models import Annotation
from app.core.annotations.models import AnnotationLabel
from app.core.annotations.schemas import AnnotationCreate
from app.core.annotations.services import annotations as annotations_service
from app.core.annotations.services.annotation_labels import list_annotation_labels
from app.core.annotations.services.annotation_labels import sync_annotation_labels
from app.core.annotations.services.annotations import assert_may_delete_annotation
from app.core.annotations.services.annotations import create_annotation
from app.core.annotations.services.annotations import get_annotation
from app.core.annotations.services.annotations import get_restorable_annotation
from app.core.annotations.services.annotations import list_annotations
from app.core.annotations.services.annotations import restore_annotation
from app.core.annotations.services.annotations import soft_delete_annotation
from app.core.config import get_settings
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Message
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.restore import restore_cutoff
from tests.core.annotations.conftest import ConversationTarget
from tests.core.annotations.conftest import persist_conversation_target
from tests.core.annotations.conftest import persist_user

pytestmark = pytest.mark.integration

_JAILBREAK = curated_label_id("jailbreak")


async def _annotate(
    db_session: AsyncSession,
    target: ConversationTarget,
    *,
    caller_id: UUID,
    label_id: UUID | None = None,
    text: str | None = None,
) -> Annotation:
    annotation, _ = await _annotate_reporting(db_session, target, caller_id=caller_id, label_id=label_id, text=text)
    return annotation


async def _annotate_reporting(
    db_session: AsyncSession,
    target: ConversationTarget,
    *,
    caller_id: UUID,
    label_id: UUID | None = None,
    text: str | None = None,
) -> tuple[Annotation, bool]:
    if label_id is None and text is None:
        label_id = _JAILBREAK
    draft = AnnotationCreate(message_id=target.messages[0].id, label_id=label_id, text=text)
    return await create_annotation(db_session, draft, caller_id=caller_id)


async def _list(db_session: AsyncSession, *, caller_id: UUID, **filters) -> tuple[list[Annotation], int]:
    return await list_annotations(
        db_session,
        caller_id=caller_id,
        filters=AnnotationFilters(**filters),
        order_by="created_at",
        limit=100,
        offset=0,
        deleted_cutoff=restore_cutoff(get_settings()),
    )


async def test_create_needs_no_conversation_ownership(db_session: AsyncSession) -> None:
    """The entity's point: an annotator labels a message of a conversation they do not own."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    annotator = await persist_user(db_session)

    annotation = await _annotate(db_session, target, caller_id=annotator.id)

    assert annotation.created_by_id == annotator.id
    assert annotation.conversation_id == target.conversation.id
    assert annotation.evaluation_id == target.evaluation.id
    assert annotation.evaluation_group_id == target.group.id
    assert annotation.label is not None
    assert annotation.label.key == "jailbreak"


async def test_create_rejects_invisible_group(db_session: AsyncSession) -> None:
    """An `invitation_only` group the caller holds no role in reads as missing, not forbidden."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    outsider = await persist_user(db_session)

    with pytest.raises(NotFoundError):
        await _annotate(db_session, target, caller_id=outsider.id)


async def test_create_rejects_an_unknown_or_retired_label(db_session: AsyncSession) -> None:
    """The picker never offers a retired label, so referencing one reads as missing."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)

    with pytest.raises(NotFoundError):
        await _annotate(db_session, target, caller_id=caller.id, label_id=uuid4())

    retired = (
        await db_session.execute(select(AnnotationLabel).where(col(AnnotationLabel.id) == _JAILBREAK))
    ).scalar_one()
    retired.soft_delete(uuid4())
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await _annotate(db_session, target, caller_id=caller.id, label_id=_JAILBREAK)


async def test_duplicate_create_returns_the_existing_row(db_session: AsyncSession) -> None:
    """Same message, same label, same author = the same intent — idempotent, not 409."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)

    first = await _annotate(db_session, target, caller_id=caller.id)
    second = await _annotate(db_session, target, caller_id=caller.id)

    assert second.id == first.id


async def test_adhoc_duplicate_is_case_insensitive(db_session: AsyncSession) -> None:
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)

    first = await _annotate(db_session, target, caller_id=caller.id, text="Prompt Injection")
    second = await _annotate(db_session, target, caller_id=caller.id, text="prompt injection")

    assert second.id == first.id
    # One label row, keeping the first spelling — the fold lives on the label now.
    assert second.label.name == "Prompt Injection"
    assert second.label.created_by_id == caller.id


async def test_typing_a_label_twice_in_either_case_is_one_annotation(db_session: AsyncSession) -> None:
    """End to end over the fold, which lives on the label row (`ix_annotation_labels_author_name`).

    `'İSTANBUL'.lower()` is `'i̇stanbul'` in Python (i + U+0307) but `'istanbul'` under the
    database's collation, so a Python-side fold would resolve the two spellings to *different*
    labels and leave the message carrying both. The label service's own test covers the fold
    directly; this one pins the behaviour a caller actually sees.
    """
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)

    first = await _annotate(db_session, target, caller_id=caller.id, text="istanbul")
    second = await _annotate(db_session, target, caller_id=caller.id, text="İSTANBUL")

    assert second.id == first.id


async def test_picking_a_curated_label_then_typing_its_name_is_one_annotation(
    db_session: AsyncSession,
) -> None:
    """The two paths to the same wording must not diverge into two labels on one message.

    The annotation unique key is `(message, label, author)`, so a typed row of its own would
    keep both annotations live — one message carrying "Jailbreak" twice from one annotator.
    """
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)

    picked = await _annotate(db_session, target, caller_id=caller.id, label_id=_JAILBREAK)
    typed = await _annotate(db_session, target, caller_id=caller.id, text="Jailbreak")

    assert typed.id == picked.id
    assert typed.label_id == _JAILBREAK


async def test_create_reports_whether_it_inserted(db_session: AsyncSession) -> None:
    """The flag the route audits on — a repeat must not record a second `annotation.create`."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)

    first, first_created = await _annotate_reporting(db_session, target, caller_id=caller.id)
    second, second_created = await _annotate_reporting(db_session, target, caller_id=caller.id)

    assert first_created is True
    assert second_created is False
    assert second.id == first.id


async def test_create_resolves_an_insert_race_to_the_winning_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `IntegrityError` arm: the loser's pre-check missed, so it re-reads the winner.

    Forced with a named label, whose resolution runs through the label table before the
    annotation is written — the longer of the two paths into this arm. The arm is only
    reachable via the monkeypatch below; what it pins is that losing the race re-reads the
    winner instead of escaping as a 500.
    """
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)
    winner = await _annotate(db_session, target, caller_id=caller.id, text="jailbreak")

    real = annotations_service._get_live_duplicate
    calls = {"n": 0}

    async def fake_get(session_, message_id, label_id, *, caller_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real(session_, message_id, label_id, caller_id=caller_id)

    monkeypatch.setattr(annotations_service, "_get_live_duplicate", fake_get)

    result, created = await _annotate_reporting(db_session, target, caller_id=caller.id, text="jailbreak")

    assert result.id == winner.id
    assert created is False
    assert calls["n"] == 2  # pre-insert check + post-IntegrityError re-read


async def test_same_label_from_two_authors_is_two_rows(db_session: AsyncSession) -> None:
    """The author is part of the identity — consensus is counted, not collapsed."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    first_author = await persist_user(db_session)
    second_author = await persist_user(db_session)

    first = await _annotate(db_session, target, caller_id=first_author.id)
    second = await _annotate(db_session, target, caller_id=second_author.id)

    assert first.id != second.id


async def test_soft_deleted_duplicate_frees_the_slot(db_session: AsyncSession) -> None:
    """The uniques are partial on live rows, so delete + re-annotate is a fresh row."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)
    first = await _annotate(db_session, target, caller_id=caller.id)
    await soft_delete_annotation(db_session, first, by_id=caller.id)

    second = await _annotate(db_session, target, caller_id=caller.id)

    assert second.id != first.id


async def test_reads_are_not_author_scoped(db_session: AsyncSession) -> None:
    """Any reader of the group sees every annotation in it — unlike notes."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    reader = await persist_user(db_session)
    created = await _annotate(db_session, target, caller_id=author.id)

    fetched = await get_annotation(db_session, created.id, caller_id=reader.id)
    listed, total = await _list(db_session, caller_id=reader.id, conversation_id=target.conversation.id)

    assert fetched.id == created.id
    assert total == 1
    assert listed[0].created_by_id == author.id


async def test_losing_group_visibility_hides_the_annotation(db_session: AsyncSession) -> None:
    """No author scope, but the visibility spine still applies; the break-glass lifts it."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    created = await _annotate(db_session, target, caller_id=author.id)
    target.group.access_level = EvaluationGroupAccessLevel.INVITATION_ONLY
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await get_annotation(db_session, created.id, caller_id=author.id)

    fetched = await get_annotation(db_session, created.id, caller_id=author.id, can_manage=True)
    assert fetched.id == created.id


async def test_superseded_message_keeps_its_annotations_listed(db_session: AsyncSession) -> None:
    """The transcript no longer renders a superseded message; the aggregation still counts it."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)
    created = await _annotate(db_session, target, caller_id=caller.id)

    superseding = Message(
        turn_id=target.messages[0].turn_id,
        role=MessageRole.ASSISTANT,
        status=MessageStatus.COMPLETE,
        content="regenerated reply",
        replaces_message_id=target.messages[0].id,
    )
    db_session.add(superseding)
    await db_session.flush()

    listed, total = await _list(db_session, caller_id=caller.id, conversation_id=target.conversation.id)

    assert total == 1
    assert listed[0].id == created.id


async def test_a_retired_label_keeps_rendering_on_existing_rows(db_session: AsyncSession) -> None:
    """Retiring a label empties the picker of it, not the annotations that carry it."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)
    created = await _annotate(db_session, target, caller_id=caller.id)

    label = (
        await db_session.execute(select(AnnotationLabel).where(col(AnnotationLabel.id) == _JAILBREAK))
    ).scalar_one()
    label.soft_delete(uuid4())
    await db_session.flush()

    fetched = await get_annotation(db_session, created.id, caller_id=caller.id)

    assert fetched.label is not None
    assert fetched.label.key == "jailbreak"


async def test_label_filter_narrows_the_listing(db_session: AsyncSession) -> None:
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)
    tagged = await _annotate(db_session, target, caller_id=caller.id)
    await _annotate(db_session, target, caller_id=caller.id, text="ad hoc")

    listed, total = await _list(db_session, caller_id=caller.id, label_id=_JAILBREAK)

    assert total == 1
    assert listed[0].id == tagged.id
    assert len(CURATED_ANNOTATION_LABELS) > 1  # the filter did the narrowing, not the data


async def test_delete_is_author_scoped_with_a_403(db_session: AsyncSession) -> None:
    """A foreign annotation is readable, so refusing its deletion must not pretend absence."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    reader = await persist_user(db_session)
    created = await _annotate(db_session, target, caller_id=author.id)
    fetched = await get_annotation(db_session, created.id, caller_id=reader.id, for_update=True)

    with pytest.raises(ForbiddenError):
        assert_may_delete_annotation(fetched, caller_id=reader.id, can_manage=False)

    assert_may_delete_annotation(fetched, caller_id=reader.id, can_manage=True)
    assert_may_delete_annotation(fetched, caller_id=author.id, can_manage=False)


async def test_restore_is_scoped_to_the_deleter_and_window(db_session: AsyncSession) -> None:
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    author = await persist_user(db_session)
    other = await persist_user(db_session)
    created = await _annotate(db_session, target, caller_id=author.id)
    await soft_delete_annotation(db_session, created, by_id=author.id)
    cutoff = restore_cutoff(get_settings())

    with pytest.raises(NotFoundError):
        await get_restorable_annotation(
            db_session, created.id, caller_id=other.id, can_manage=False, deleted_cutoff=cutoff
        )

    restorable = await get_restorable_annotation(
        db_session, created.id, caller_id=author.id, can_manage=False, deleted_cutoff=cutoff
    )
    restored = await restore_annotation(db_session, restorable)

    assert restored.deleted_at is None
    assert restored.deleted_by_id is None


async def test_restore_conflicts_with_a_duplicate_created_since(db_session: AsyncSession) -> None:
    """Delete, re-annotate, then restore the tombstone: the partial unique refuses the revival."""
    await sync_annotation_labels(db_session)
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)
    first = await _annotate(db_session, target, caller_id=caller.id)
    await soft_delete_annotation(db_session, first, by_id=caller.id)
    await _annotate(db_session, target, caller_id=caller.id)
    cutoff = restore_cutoff(get_settings())

    restorable = await get_restorable_annotation(
        db_session, first.id, caller_id=caller.id, can_manage=False, deleted_cutoff=cutoff
    )
    with pytest.raises(ConflictError):
        await restore_annotation(db_session, restorable)


async def test_restore_conflicts_when_a_custom_label_was_re_used(db_session: AsyncSession) -> None:
    """The same conflict over a *custom* label, which resolves through the per-author row.

    Both spellings collapse onto one label id before reaching `annotations`, so the restore
    hits `ix_annotations_message_label_author` — the single dedup index — rather than the
    service's pre-check.
    """
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)
    first = await _annotate(db_session, target, caller_id=caller.id, text="Prompt Injection")
    await soft_delete_annotation(db_session, first, by_id=caller.id)
    # A different spelling resolves to the same label row, so the annotation is a duplicate.
    await _annotate(db_session, target, caller_id=caller.id, text="prompt injection")
    cutoff = restore_cutoff(get_settings())

    restorable = await get_restorable_annotation(
        db_session, first.id, caller_id=caller.id, can_manage=False, deleted_cutoff=cutoff
    )
    with pytest.raises(ConflictError):
        await restore_annotation(db_session, restorable)


async def test_a_colleagues_label_is_offered_within_the_conversation_they_used_it_in(
    db_session: AsyncSession,
) -> None:
    """The convergence arm: annotators on one transcript see each other's spellings.

    Without it two annotators labelling the same conversation invent parallel wordings; with
    it the second is offered the first's label instead of typing a near-duplicate. Scoped to
    the conversation, so it is not a licence to browse someone's whole private pool.
    """
    target = await persist_conversation_target(db_session)
    first_author = await persist_user(db_session)
    second_author = await persist_user(db_session)
    theirs = await _annotate(db_session, target, caller_id=first_author.id, text="roleplay bypass")

    scoped, _ = await list_annotation_labels(
        db_session, caller_id=second_author.id, conversation_id=target.conversation.id, limit=100, offset=0
    )
    unscoped, _ = await list_annotation_labels(db_session, caller_id=second_author.id, limit=100, offset=0)

    assert theirs.label_id in [label.id for label in scoped]
    assert theirs.label_id not in [label.id for label in unscoped]


async def test_one_wording_is_offered_once_however_many_annotators_hold_it(
    db_session: AsyncSession,
) -> None:
    """Each annotator gets their own row for a shared spelling, so the arm would repeat it.

    `total` is what makes this a contract bug rather than cosmetics: it counted every copy, and
    duplicates could straddle a page, so no client-side dedupe could repair the listing.
    """
    target = await persist_conversation_target(db_session)
    first_author = await persist_user(db_session)
    second_author = await persist_user(db_session)
    third_author = await persist_user(db_session)
    theirs = await _annotate(db_session, target, caller_id=first_author.id, text="roleplay bypass")
    # Picking a colleague's label copies it to the picker's own row — that is the multiplication.
    await _annotate(db_session, target, caller_id=second_author.id, label_id=theirs.label_id)

    labels, total = await list_annotation_labels(
        db_session, caller_id=third_author.id, conversation_id=target.conversation.id, limit=100, offset=0
    )

    offered = [label.name for label in labels]
    assert offered.count("roleplay bypass") == 1
    # Nothing here syncs the catalog and the conftest seeds no curated rows, so 1 is the whole table.
    assert total == 1


async def test_the_callers_own_row_outranks_a_colleagues_of_the_same_wording(
    db_session: AsyncSession,
) -> None:
    """The everyday precedence contest, since resolution hands curated the wording first.

    Both rows are on this conversation, so both arms match; offering the colleague's would hand
    the caller an id that `_resolve_label` immediately maps back to their own.
    """
    target = await persist_conversation_target(db_session)
    caller = await persist_user(db_session)
    colleague = await persist_user(db_session)
    theirs = await _annotate(db_session, target, caller_id=colleague.id, text="roleplay bypass")
    ours = await _annotate(db_session, target, caller_id=caller.id, label_id=theirs.label_id)
    assert ours.label_id != theirs.label_id  # precondition: the pick was copied to our own row

    labels, _ = await list_annotation_labels(
        db_session, caller_id=caller.id, conversation_id=target.conversation.id, limit=100, offset=0
    )

    ids = [label.id for label in labels]
    assert ours.label_id in ids
    assert theirs.label_id not in ids


async def test_the_conversation_arm_respects_the_group_boundary(db_session: AsyncSession) -> None:
    """Knowing a conversation id must not be enough — it has to be one the caller can see.

    Without the shared visibility scope on that arm, any `annotations:read` holder could read
    the label names of an `invitation_only` group they hold no role in. The break-glass still
    lifts it, as it does every other annotation read.
    """
    target = await persist_conversation_target(db_session, access_level=EvaluationGroupAccessLevel.INVITATION_ONLY)
    outsider = await persist_user(db_session)
    # The group's creator is the one identity that can see an `invitation_only` group here.
    theirs = await _annotate(db_session, target, caller_id=target.group.created_by_id, text="behind the wall")

    hidden, _ = await list_annotation_labels(
        db_session, caller_id=outsider.id, conversation_id=target.conversation.id, limit=100, offset=0
    )
    with_break_glass, _ = await list_annotation_labels(
        db_session,
        caller_id=outsider.id,
        can_manage=True,
        conversation_id=target.conversation.id,
        limit=100,
        offset=0,
    )

    assert theirs.label_id not in [label.id for label in hidden]
    assert theirs.label_id in [label.id for label in with_break_glass]


async def test_a_colleagues_offered_label_resolves_to_the_callers_own_row(db_session: AsyncSession) -> None:
    """The offer must be usable: the picker shows it, so create must not 404 on it.

    It resolves to *this* caller's row of the same wording rather than attaching theirs, so
    the per-author unique can still dedupe — one annotator cannot end up with two
    identically-named labels on one message. Convergence is therefore on the spelling, not on
    a shared row.
    """
    target = await persist_conversation_target(db_session)
    first_author = await persist_user(db_session)
    second_author = await persist_user(db_session)
    theirs = await _annotate(db_session, target, caller_id=first_author.id, text="roleplay bypass")

    mine = await _annotate(db_session, target, caller_id=second_author.id, label_id=theirs.label_id)

    assert mine.label_id != theirs.label_id
    assert mine.label.name == "roleplay bypass"
    assert mine.label.created_by_id == second_author.id


async def test_a_label_from_another_conversation_reads_as_missing(db_session: AsyncSession) -> None:
    """Only labels offered *here* are attachable — nothing else confirms a colleague's wording."""
    here = await persist_conversation_target(db_session)
    elsewhere = await persist_conversation_target(db_session)
    owner_of_label = await persist_user(db_session)
    other = await persist_user(db_session)
    theirs = await _annotate(db_session, elsewhere, caller_id=owner_of_label.id, text="private wording")

    with pytest.raises(NotFoundError):
        await _annotate(db_session, here, caller_id=other.id, label_id=theirs.label_id)


async def test_a_soft_deleted_annotation_stops_offering_its_label(db_session: AsyncSession) -> None:
    """The picker and create must agree, and the liveness filter has to be explicit to do it.

    `live_select()` carries its soft-delete filter as a loader-criteria *option*, which
    SQLAlchemy drops when the statement is nested as an `IN` subquery — as the conversation arm
    nests it. Relying on that option left a colleague's label offered after its only annotation
    here was deleted, and `create` then refused it as missing.
    """
    target = await persist_conversation_target(db_session)
    first_author = await persist_user(db_session)
    second_author = await persist_user(db_session)
    theirs = await _annotate(db_session, target, caller_id=first_author.id, text="roleplay bypass")

    offered_before, _ = await list_annotation_labels(
        db_session, caller_id=second_author.id, conversation_id=target.conversation.id, limit=100, offset=0
    )
    await soft_delete_annotation(db_session, theirs, by_id=first_author.id)
    offered_after, _ = await list_annotation_labels(
        db_session, caller_id=second_author.id, conversation_id=target.conversation.id, limit=100, offset=0
    )

    assert theirs.label_id in [label.id for label in offered_before]
    assert theirs.label_id not in [label.id for label in offered_after]
    # And the two stay consistent: what is no longer offered is no longer attachable either.
    with pytest.raises(NotFoundError):
        await _annotate(db_session, target, caller_id=second_author.id, label_id=theirs.label_id)
