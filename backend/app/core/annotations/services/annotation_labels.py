"""Annotation labels: the curated-catalog sync, the per-author create path, and the reads.

Two kinds of row, `DataLicense`-style. **Curated** (`created_by_id IS NULL`) come from
`label_catalog.py` via `sync_annotation_labels`, alongside `sync_roles` / `sync_licenses` as
the platform's reference-data syncs. **User-scoped** (`created_by_id` set) are created on
demand by `find_or_create_user_label` when an annotator names a label new to them *and*
absent from the curated set — so a typed label persists as *their* entry rather than being
retyped per message, without ever entering the shared vocabulary.
"""

from uuid import UUID

from sqlalchemy import Case
from sqlalchemy import ColumnElement
from sqlalchemy import Select
from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.util import AliasedClass
from sqlmodel import col

from app.core.annotations.label_catalog import CURATED_ANNOTATION_LABELS
from app.core.annotations.label_catalog import curated_label_id
from app.core.annotations.models import Annotation
from app.core.annotations.models import AnnotationLabel
from app.core.evaluations.access import join_conversation_scoped
from app.core.helpers import sanitise_single_line
from app.core.pagination import paginate


async def sync_annotation_labels(session: AsyncSession) -> int:
    """Idempotently upsert the curated label catalog, returning the number of catalog entries.

    Keyed by the deterministic `curated_label_id`, so a re-run reconciles the wording of an
    existing entry instead of inserting a second row, and revives a tombstoned one.
    Mirrors `syncroles` / `synclicenses`: a deploy step after `migrate`, and part of `seedlocal`.

    Refuses to write a wording that some **other** live row already holds — a label an annotator
    typed, or a curated row this catalog no longer names. Both leave one wording as two ids, which
    the annotation unique key `(message, label, author)` cannot see, so one annotator could carry
    that wording twice on a message. Which row survives, and where the annotations already pointing
    at it go, is a data decision; the deploy stops and asks rather than guessing.

    It does not close every route there, and the two it misses are why the unit test and the
    `_precedence` note exist: two catalog entries agreeing up to case are both *shipped* ids, so
    they escape this scan entirely (`test_annotation_label_catalog` is what catches them), and the
    scan and the upsert below take no lock.

    Folded in Postgres on **both** sides, like everything else that compares these names: a
    Python-side fold disagrees on dotted capitals, so the `IN` list would hold values the
    column can never equal and the guard would pass on exactly the input it exists to catch.

    A guard rather than a partial unique on `lower(name) WHERE created_by_id IS NULL`, which would
    make the curated rule unforgeable the way `ix_annotation_labels_author_name` makes the
    per-author one: the upsert below walks the catalog entry by entry in a single transaction, so
    swapping two entries' wordings would trip such an index halfway through on a state that is
    legal once the loop ends. It also names every offending row with its id, where a unique
    violation reports one duplicate value.

    Equality here is `lower()`, so Unicode normalization is out of scope: a curated `Café` in
    NFC and a typed one in NFD are two rows that look identical and both attach to one message.
    Accepted while the catalog is ours.

    Raises:
        RuntimeError: If any other live row shares a folded name with a catalog entry.
    """
    shipped = {curated_label_id(label.key) for label in CURATED_ANNOTATION_LABELS}
    wordings = [func.lower(label.name) for label in CURATED_ANNOTATION_LABELS]
    collisions = (
        await session.execute(
            AnnotationLabel.live_select()
            .where(func.lower(col(AnnotationLabel.name)).in_(wordings))
            .where(col(AnnotationLabel.id).notin_(shipped))
        )
    ).scalars()
    clashing = sorted(f"{row.name!r} ({row.id})" for row in collisions)
    if clashing:
        raise RuntimeError(
            f"Curated annotation labels collide with live rows: {', '.join(clashing)}. "
            "Retire or rename those rows by hand (SQL — there is no write route), or change the "
            "catalog wording, then re-run syncannotationlabels."
        )

    for label in CURATED_ANNOTATION_LABELS:
        values = {
            "id": curated_label_id(label.key),
            "key": label.key,
            "name": label.name,
            # Re-asserted, as `sync_licenses` does: a resync restores curatedness rather than
            # leaving whatever the row happened to hold.
            "created_by_id": None,
        }
        updates = {k: v for k, v in values.items() if k != "id"}
        await session.execute(
            pg_insert(AnnotationLabel)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["id"],
                # Clear both tombstone columns, as `BaseModel.restore` does — a revived
                # curated row must not keep the actor who deleted it.
                set_=updates | {"deleted_at": None, "deleted_by_id": None, "updated_at": func.now()},
            )
        )
    await session.flush()
    return len(CURATED_ANNOTATION_LABELS)


async def find_or_create_user_label(session: AsyncSession, name: str, *, caller_id: UUID) -> AnnotationLabel:
    """The row this caller's ``name`` resolves to — the curated one, theirs, or a new one.

    Reuse before create, so naming the same label on a second message returns the first row
    rather than a duplicate. Matching folds case **in Postgres**, which is also where the
    partial unique folds it: Python disagrees with the database on dotted capitals
    (`'İSTANBUL'`), so a Python-side fold would miss what the index catches and turn a
    plausible input into an unhandled `IntegrityError`.

    **Curated first.** Typing the shared vocabulary's wording reaches the shared row, so
    picking it and typing it cannot diverge — the annotation unique key is
    `(message, label, author)`, and two rows of one wording would let a single annotator put
    that wording on a message twice. Referencing a curated row is what the pick path already
    does; it does not edit the curated set.

    Curated ahead of the caller's own is what makes the vocabulary *converge*. The reverse
    would freeze a permanent split: an annotator who typed "Jailbreak" before the catalog
    shipped it would keep their private row forever while colleagues used the shared one, and
    counting that label across annotators would go back to matching strings. Annotations made
    before the catalog added or reworded that entry keep pointing at the row they were created
    with — a state `sync_annotation_labels` refuses to create, save across its own scan/upsert
    window (see `_precedence`).
    """
    cleaned = sanitise_single_line(name)
    shared = (
        AnnotationLabel.live_select()
        .where(col(AnnotationLabel.created_by_id).is_(None))
        .where(func.lower(col(AnnotationLabel.name)) == func.lower(cleaned))
        # Two live curated rows can share a wording: hand SQL, or two catalog entries agreeing up
        # to case — which `test_annotation_label_catalog` prevents, not this guard, since the guard
        # skips shipped ids. `list_annotation_labels` serves that state on its anti-join's `id`
        # tiebreak, so take the same survivor rather than raising `MultipleResultsFound` and
        # 500-ing every annotate of a wording the read beside it renders.
        .order_by(col(AnnotationLabel.id))
        .limit(1)
    )
    curated = (await session.execute(shared)).scalars().first()
    if curated is not None:
        return curated

    mine = (
        AnnotationLabel.live_select()
        .where(col(AnnotationLabel.created_by_id) == caller_id)
        .where(func.lower(col(AnnotationLabel.name)) == func.lower(cleaned))
    )
    existing = (await session.execute(mine)).scalar_one_or_none()
    if existing is not None:
        return existing

    label = AnnotationLabel(name=cleaned, created_by_id=caller_id)
    try:
        async with session.begin_nested():
            session.add(label)
            await session.flush()
    except IntegrityError:
        # Lost the race on `ix_annotation_labels_author_name`; the concurrent insert's row is
        # authoritative. Because the author is in the index, that row is necessarily this
        # caller's, so the re-read cannot hand back someone else's label.
        won = (await session.execute(mine)).scalar_one_or_none()
        if won is None:
            raise
        return won
    await session.refresh(label, attribute_names=["created_at", "updated_at"])
    return label


def labels_used_in_conversation(conversation_id: UUID, *, caller_id: UUID, can_manage: bool) -> Select[tuple[UUID]]:
    """Ids of the labels carried by annotations on one conversation the caller can see.

    Author-blind (`author_scoped=False`) because the point is convergence — a colleague's
    wording on this transcript is exactly what should be offered — but never visibility-blind:
    the scope goes through the shared `join_conversation_scoped`, so an unreachable or
    soft-deleted conversation yields nothing rather than leaking its vocabulary.

    The annotation's own liveness is an **explicit** predicate, not `live_select()`'s: that
    filter rides as a `with_loader_criteria` option, which SQLAlchemy drops when the statement
    is nested as an `IN` subquery — as it is here. Relying on it made the picker offer a label
    whose only annotation had been soft-deleted, which `create` then refused as missing: the
    two disagreed while appearing to share one scope. `soft_delete.py` documents the option as
    propagating to eager loads and aliases, not into nested selects.
    """
    scoped = (
        join_conversation_scoped(
            select(Annotation),
            Annotation,
            caller_id=caller_id,
            can_manage=can_manage,
            author_scoped=False,
        )
        .where(col(Annotation.conversation_id) == conversation_id)
        .where(col(Annotation.deleted_at).is_(None))
    )
    # The scope is expressed over the entity (that is what the shared helper is typed for), so
    # narrow to the id afterwards; `maintain_column_froms` keeps the visibility joins, as the
    # pagination helper does for its count.
    return scoped.with_only_columns(col(Annotation.label_id), maintain_column_froms=True)


def _offered(
    entity: type[AnnotationLabel] | AliasedClass[AnnotationLabel],
    *,
    caller_id: UUID,
    can_manage: bool,
    conversation_id: UUID | None,
) -> ColumnElement[bool]:
    """The offer arms as one condition, so the dedupe below scopes exactly like the page does."""
    arms = [col(entity.created_by_id).is_(None), col(entity.created_by_id) == caller_id]
    if conversation_id is not None:
        arms.append(
            col(entity.id).in_(labels_used_in_conversation(conversation_id, caller_id=caller_id, can_manage=can_manage))
        )
    return or_(*arms)


def _precedence(entity: type[AnnotationLabel] | AliasedClass[AnnotationLabel], *, caller_id: UUID) -> Case[int]:
    """Which row wins when several share a wording: curated, then the caller's own, then a colleague's.

    The same order `find_or_create_user_label` resolves in for the two tiers they share, so
    picking and typing agree; a colleague's row is a third tier only the picker has, which
    `_resolve_label` collapses onto the caller's own after the fact. Curated first because the
    shared vocabulary is what aggregates by id.

    The sync guard makes curated and the caller's own sharing a wording rare, not impossible, so
    rank 0 beating rank 1 stays load-bearing: the guard's scan and its upsert are separate
    statements with no lock, and `deploy.sh` runs the sync *before* rolling the containers — so
    the outgoing stack can mint a user row for the wording being added. In the ordinary case what
    this decides is that either beats a colleague's.
    """
    return case(
        (col(entity.created_by_id).is_(None), 0),
        (col(entity.created_by_id) == caller_id, 1),
        else_=2,
    )


async def list_annotation_labels(
    session: AsyncSession,
    *,
    caller_id: UUID,
    can_manage: bool = False,
    conversation_id: UUID | None = None,
    limit: int,
    offset: int,
) -> tuple[list[AnnotationLabel], int]:
    """Return one page of the labels this caller may pick from, ordered by display name.

    Three sources, unioned: the **curated** vocabulary, the caller's **own** labels (so one
    they named on another conversation is still offered here), and — when
    ``conversation_id`` is given — every label **already used in that conversation**, whoever
    typed it, so annotators working the same transcript converge on one spelling instead of
    inventing parallel ones.

    Deliberately *not* every user's labels: another annotator's private vocabulary is not
    this caller's to browse, and unioning it wholesale would leak label names across groups.
    The conversation arm goes through `join_conversation_scoped` — the same authorization home
    every other annotation read uses — so knowing a conversation's id is not enough: it has to
    be one the caller can see, and its ancestry has to be live. Without that join the arm would
    hand any `annotations:read` holder the label names of an `invitation_only` group they are
    not in.

    Ordered by `name`, not `key`: the caller is a picker showing the wording. A tombstoned
    entry is retired, so it never appears.

    **One entry per wording.** The conversation arm surfaces every annotator's row, and
    `_resolve_label` gives each of them their own, so N annotators on one spelling would
    otherwise be N identical-looking options — with `total` counting them all and the copies
    able to straddle a page, which no client-side dedupe can repair. The survivor is chosen by
    `_precedence`. Expressed as a correlated `NOT EXISTS` rather than `DISTINCT ON`, which
    `paginate` cannot carry: its count swaps the columns clause for `COUNT(*)`, and
    `DISTINCT ON (lower(name)) count(*)` is not a valid aggregate query.
    """
    scope = {"caller_id": caller_id, "can_manage": can_manage, "conversation_id": conversation_id}
    better = aliased(AnnotationLabel)
    mine = _precedence(AnnotationLabel, caller_id=caller_id)
    theirs = _precedence(better, caller_id=caller_id)
    outranked = (
        select(col(better.id))
        # Redundant with the outer `live_select()` option, which does reach an alias
        # (`include_aliases`); kept so the anti-join states its own scope rather than
        # depending on that. Unlike `labels_used_in_conversation`, where the option belongs to
        # a *nested statement* and is genuinely dropped, this alias is governed by the outer one.
        .where(col(better.deleted_at).is_(None))
        .where(func.lower(col(better.name)) == func.lower(col(AnnotationLabel.name)))
        .where(_offered(better, **scope))
        .where(or_(theirs < mine, and_(theirs == mine, col(better.id) < col(AnnotationLabel.id))))
    )
    statement = (
        AnnotationLabel.live_select()
        .where(_offered(AnnotationLabel, **scope))
        .where(~exists(outranked))
        .order_by(func.lower(col(AnnotationLabel.name)), col(AnnotationLabel.id))
    )
    return await paginate(session, statement, limit=limit, offset=offset)
