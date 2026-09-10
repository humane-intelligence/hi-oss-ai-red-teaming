"""Integration tests for the annotation-label catalog sync and the label read/create services.

Unlike the curated licences, these rows are not seeded per worker (nothing resolves through
them), so each test syncs explicitly and starts from an empty table.
"""

from uuid import UUID
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.annotations.label_catalog import CURATED_ANNOTATION_LABELS
from app.core.annotations.label_catalog import curated_label_id
from app.core.annotations.models import AnnotationLabel
from app.core.annotations.services.annotation_labels import find_or_create_user_label
from app.core.annotations.services.annotation_labels import list_annotation_labels
from app.core.annotations.services.annotation_labels import sync_annotation_labels
from tests.core.annotations.conftest import persist_user

pytestmark = pytest.mark.integration


async def _labels(db_session: AsyncSession) -> list[AnnotationLabel]:
    result = await db_session.execute(select(AnnotationLabel).order_by(col(AnnotationLabel.key)))
    return list(result.scalars())


async def test_sync_annotation_labels_is_idempotent(db_session: AsyncSession) -> None:
    # Keyed on the deterministic id, so a resync reconciles rather than inserting a second set.
    first = await sync_annotation_labels(db_session)
    second = await sync_annotation_labels(db_session)
    rows = await _labels(db_session)

    assert first == second == len(CURATED_ANNOTATION_LABELS)
    assert len(rows) == len(CURATED_ANNOTATION_LABELS)
    # Every synced row is curated: the sync never sets `created_by_id`, which is the whole
    # basis of telling the shared vocabulary from a label an annotator named.
    assert all(row.created_by_id is None for row in rows)
    # Curated rows all carry a key; a user-scoped label would not, so the filter is meaningful.
    assert sorted(row.key or "" for row in rows) == sorted(label.key for label in CURATED_ANNOTATION_LABELS)


async def test_sync_annotation_labels_reconciles_wording_without_moving_the_row(
    db_session: AsyncSession,
) -> None:
    # The point of keying on `key`: fixing a display name reaches the existing row, so the
    # annotations pointing at that id keep their label.
    await sync_annotation_labels(db_session)
    label = CURATED_ANNOTATION_LABELS[0]
    row_id = curated_label_id(label.key)
    await db_session.execute(
        update(AnnotationLabel).where(col(AnnotationLabel.id) == row_id).values(name="Stale wording")
    )

    await sync_annotation_labels(db_session)
    rows = await _labels(db_session)
    reconciled = next(row for row in rows if row.id == row_id)

    assert reconciled.name == label.name
    assert len(rows) == len(CURATED_ANNOTATION_LABELS)


@pytest.mark.parametrize(
    "authored", [True, False], ids=["a label an author named", "a curated row the catalog dropped"]
)
async def test_sync_annotation_labels_leaves_a_row_it_does_not_own_alone(
    db_session: AsyncSession, authored: bool
) -> None:
    """The sync reconciles its own entries and nothing else.

    Load-bearing once label CRUD exists: a label an annotator named must survive every deploy.
    Dropping an entry from the catalog tuple has to leave its row live too, so both shapes are
    covered. Neither is reachable: the sync only ever targets the id it derives from a key it
    still ships.
    """
    await sync_annotation_labels(db_session)
    # The dropped entry keeps the id its key derived — the shape the sync actually has to skip.
    # A random id would leave the guarantee resting on the id merely being unfamiliar.
    if authored:
        author = await persist_user(db_session)
        foreign = AnnotationLabel(name="Hand rolled", created_by_id=author.id)
    else:
        foreign = AnnotationLabel(id=curated_label_id("hand-rolled"), key="hand-rolled", name="Hand rolled")
    db_session.add(foreign)
    await db_session.flush()

    await sync_annotation_labels(db_session)
    await db_session.refresh(foreign)
    rows = await _labels(db_session)

    assert foreign.deleted_at is None
    assert foreign.name == "Hand rolled"
    assert len(rows) == len(CURATED_ANNOTATION_LABELS) + 1


async def test_sync_annotation_labels_clears_both_tombstone_columns(db_session: AsyncSession) -> None:
    # A revived curated row must not keep the actor who deleted it, or `deleted_by_id`
    # outlives the tombstone it belongs to.
    await sync_annotation_labels(db_session)
    row_id = curated_label_id(CURATED_ANNOTATION_LABELS[0].key)
    row = (await db_session.execute(select(AnnotationLabel).where(col(AnnotationLabel.id) == row_id))).scalar_one()
    row.soft_delete(uuid4())
    db_session.add(row)
    await db_session.flush()

    await sync_annotation_labels(db_session)
    await db_session.refresh(row)

    assert row.deleted_at is None
    assert row.deleted_by_id is None


async def test_list_annotation_labels_omits_a_retired_label(db_session: AsyncSession) -> None:
    # Retiring an entry is a soft delete: it leaves the picker while the rows that reference
    # it keep resolving. The live filter is this service's branch, so it is pinned here — the
    # API test asserts the HTTP contract only.
    await sync_annotation_labels(db_session)
    rows = await _labels(db_session)
    retired = rows[0]
    retired.soft_delete(uuid4())
    db_session.add(retired)
    await db_session.flush()

    labels, total = await list_annotation_labels(db_session, caller_id=uuid4(), limit=100, offset=0)

    assert total == len(CURATED_ANNOTATION_LABELS) - 1
    assert retired.key not in [label.key for label in labels]


async def test_find_or_create_user_label_reuses_the_authors_own(db_session: AsyncSession) -> None:
    """The point of the per-user pool: naming the same label twice is one row, not two."""
    author = await persist_user(db_session)

    first = await find_or_create_user_label(db_session, "Prompt Injection", caller_id=author.id)
    second = await find_or_create_user_label(db_session, "prompt injection", caller_id=author.id)

    assert second.id == first.id
    assert second.name == "Prompt Injection"  # the first spelling wins
    assert first.created_by_id == author.id
    assert first.key is None


async def test_find_or_create_user_label_folds_case_like_the_index(db_session: AsyncSession) -> None:
    # Postgres and Python disagree on dotted capitals: `'İSTANBUL'.lower()` is `'i̇stanbul'`
    # here and `'istanbul'` there. Folding in Python would miss what
    # `ix_annotation_labels_author_name` catches and surface as an IntegrityError.
    author = await persist_user(db_session)

    first = await find_or_create_user_label(db_session, "istanbul", caller_id=author.id)
    second = await find_or_create_user_label(db_session, "İSTANBUL", caller_id=author.id)

    assert second.id == first.id


async def test_find_or_create_user_label_is_per_author(db_session: AsyncSession) -> None:
    """Two annotators naming the same label get their own rows — the pool is scoped, not shared."""
    first_author = await persist_user(db_session)
    second_author = await persist_user(db_session)

    mine = await find_or_create_user_label(db_session, "jailbreak", caller_id=first_author.id)
    theirs = await find_or_create_user_label(db_session, "jailbreak", caller_id=second_author.id)

    assert mine.id != theirs.id


async def test_find_or_create_user_label_adopts_a_curated_entry_of_the_same_name(
    db_session: AsyncSession,
) -> None:
    """Typing the shared wording reaches the shared row, so picking and typing cannot diverge.

    Minting a second row here is what would let one annotator carry two identically-named
    labels on one message, since the annotation unique key is `(message, label, author)`.
    """
    await sync_annotation_labels(db_session)
    author = await persist_user(db_session)

    typed = await find_or_create_user_label(db_session, "Jailbreak", caller_id=author.id)

    assert typed.id == curated_label_id("jailbreak")
    assert typed.created_by_id is None


async def test_sync_refuses_a_curated_wording_a_user_label_already_holds(
    db_session: AsyncSession,
) -> None:
    """The deploy stops rather than splitting one wording across two ids.

    Only reachable by rewording a catalog entry onto a name someone typed first — nothing mints
    such a row, since typing a curated wording reuses the curated one. Writing it anyway would
    leave the annotations already on the user's row there while new ones resolved to the curated
    row, so one annotator could carry that wording twice on a message. Which row survives, and
    where the existing annotations point, is a data decision.

    Typed in a case the catalog does not use, so the guard's fold is what makes the match: with
    `func.lower` dropped from either side the collision goes unseen and the sync returns happily.
    """
    author = await persist_user(db_session)
    own = await find_or_create_user_label(db_session, "JAILBREAK", caller_id=author.id)
    assert own.created_by_id == author.id  # precondition: the catalog does not hold it yet

    with pytest.raises(RuntimeError, match="'JAILBREAK'"):
        await sync_annotation_labels(db_session)


async def test_sync_refuses_a_wording_a_stale_curated_row_still_holds(db_session: AsyncSession) -> None:
    """Re-keying an entry is the likelier collision, and it is curated-on-curated.

    The upsert has no retire path, so the row under the old key stays live with the same
    wording. A guard that only looked at *user* rows would pass here and leave one wording as
    two curated ids — both attachable by id, so one annotator could carry it twice on a message,
    and `find_or_create_user_label` would resolve it on an `id` tiebreak rather than by wording.

    The stale row keeps the wording in a different case, so this pins the fold on both sides of
    the guard the same way the user-row case above does.
    """
    await sync_annotation_labels(db_session)
    stale = AnnotationLabel(key="jailbreak-v1", name="JailBreak")
    db_session.add(stale)
    await db_session.flush()

    with pytest.raises(RuntimeError, match="'JailBreak'"):
        await sync_annotation_labels(db_session)


async def test_sync_ignores_a_tombstoned_collision(db_session: AsyncSession) -> None:
    # A retired row holds no wording, so it must not stop a deploy — the guard reads live rows
    # only, and getting that wrong would wedge every future sync on a label someone once typed.
    author = await persist_user(db_session)
    retired = await find_or_create_user_label(db_session, "Jailbreak", caller_id=author.id)
    retired.soft_delete(uuid4())
    await db_session.flush()

    assert await sync_annotation_labels(db_session) == len(CURATED_ANNOTATION_LABELS)


async def test_typing_a_wording_two_curated_rows_hold_resolves_instead_of_raising(
    db_session: AsyncSession,
) -> None:
    """The read path already survives this state; the annotate path used to 500 on it.

    Hand SQL gets here, as would two catalog entries agreeing up to case; the defect is the two
    paths disagreeing — `list_annotation_labels` picks a survivor on the `id` tiebreak while
    `scalar_one_or_none` raised `MultipleResultsFound` for every annotate of the wording.
    Cases deliberately differ from each other and from the lookup, so the fold carries the match.
    The **keys** deliberately contradict the id order, which is what pins the lookup's
    `ORDER BY id`: `ix_annotation_labels_key` is partial on this query's `created_by_id` and
    `deleted_at` predicates, so the planner serves it from that index and an unordered scan comes
    back in *key* order — a plan, not a guarantee, so if it ever flips to a seq scan this stops
    discriminating.
    """
    author = await persist_user(db_session)
    first = AnnotationLabel(id=UUID(int=1), key="jailbreak-z", name="Jailbreak")
    second = AnnotationLabel(id=UUID(int=2), key="jailbreak-a", name="jailbreak")
    db_session.add_all([first, second])
    await db_session.flush()

    resolved = await find_or_create_user_label(db_session, "JAILBREAK", caller_id=author.id)

    offered, total = await list_annotation_labels(db_session, caller_id=author.id, limit=100, offset=0)
    assert resolved.id == first.id
    assert [label.id for label in offered] == [resolved.id]
    assert total == 1


async def test_a_curated_row_is_offered_ahead_of_a_users_of_the_same_wording(
    db_session: AsyncSession,
) -> None:
    """Pins `_precedence`'s ranking of the two tiers the sync guard keeps apart.

    Ordinary use does not produce this state — that is the guard's whole point — so it is built
    directly. Without this the ranking could be reversed and every other test would pass.
    """
    await sync_annotation_labels(db_session)
    author = await persist_user(db_session)
    db_session.add(AnnotationLabel(name="Jailbreak", created_by_id=author.id))
    await db_session.flush()

    labels, _ = await list_annotation_labels(db_session, caller_id=author.id, limit=100, offset=0)

    jailbreak = [label for label in labels if label.name.lower() == "jailbreak"]
    assert len(jailbreak) == 1
    assert jailbreak[0].id == curated_label_id("jailbreak")


async def test_list_offers_curated_plus_the_callers_own_but_not_another_users(
    db_session: AsyncSession,
) -> None:
    """The scoping rule: another annotator's private pool is not this caller's to browse."""
    await sync_annotation_labels(db_session)
    caller = await persist_user(db_session)
    other = await persist_user(db_session)
    mine = await find_or_create_user_label(db_session, "my own label", caller_id=caller.id)
    theirs = await find_or_create_user_label(db_session, "their own label", caller_id=other.id)

    labels, total = await list_annotation_labels(db_session, caller_id=caller.id, limit=100, offset=0)
    ids = [label.id for label in labels]

    assert total == len(CURATED_ANNOTATION_LABELS) + 1
    assert mine.id in ids
    assert theirs.id not in ids


async def test_a_resync_over_a_hand_re_added_key_aborts(db_session: AsyncSession) -> None:
    """A retired entry plus a hand-added row on its key takes the next deploy's sync down.

    The tombstone sits at the id the catalog derives, so the resync's conflict arm revives it —
    and the live duplicate then leaves two curated rows sharing a key, which the partial unique
    index rejects. Only reachable through hand-written SQL, but the deploy's sync step is fatal.

    The hand-added row is deliberately named something the catalog does not ship: with the
    catalog's own wording it never reaches the index, because the name guard above refuses the
    sync first. This test is here for the key path, so it has to isolate it.
    """
    await sync_annotation_labels(db_session)
    retired = (
        await db_session.execute(
            select(AnnotationLabel).where(col(AnnotationLabel.id) == curated_label_id("jailbreak"))
        )
    ).scalar_one()
    retired.soft_delete(uuid4())
    await db_session.flush()
    db_session.add(AnnotationLabel(key="jailbreak", name="A rival wording"))
    await db_session.flush()

    with pytest.raises(IntegrityError):
        await sync_annotation_labels(db_session)
