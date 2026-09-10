"""Saved-views service — pure async functions over an `AsyncSession`.

Saved views are strictly **owner-scoped** personal data: a caller reads,
updates, and deletes only views they created (`created_by_id == caller_id`).
There is no admin break-glass — one user's saved views are of no concern to
another. `state` arrives already validated as a `SavedViewState` envelope and is
stored as a plain dict in the JSONB column (this layer treats it opaquely; the
API layer owns the shape). `(resource, name)` is unique per owner over live rows
(a partial index); a duplicate is a 409.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.ordering import apply_order_by
from app.core.pagination import paginate
from app.core.restore import deleted_select
from app.core.restore import restore_row
from app.core.saved_views.filters import SavedViewFilters
from app.core.saved_views.filters import SavedViewOrderBy
from app.core.saved_views.models import SavedView
from app.core.saved_views.schemas import SavedViewCreate
from app.core.saved_views.schemas import SavedViewUpdateChanges


def _scope(statement: Select[tuple[SavedView]], *, caller_id: UUID) -> Select[tuple[SavedView]]:
    """Constrain to the caller's own views — the owner boundary, in one place."""
    return statement.where(col(SavedView.created_by_id) == caller_id)


async def get_saved_view(
    session: AsyncSession, view_id: UUID, *, caller_id: UUID, for_update: bool = False
) -> SavedView:
    """Fetch one live saved view ``view_id`` owned by the caller.

    A view owned by another user reads as missing (404, no existence leak).
    Mutation paths pass ``for_update=True`` to lock the row for the write.

    Raises:
        NotFoundError: If no such view is owned by the caller.
    """
    statement = _scope(SavedView.live_select().where(col(SavedView.id) == view_id), caller_id=caller_id)
    if for_update:
        statement = statement.with_for_update()
    view = (await session.execute(statement)).scalar_one_or_none()
    if view is None:
        raise NotFoundError(f"Saved view {view_id} not found.")
    return view


async def list_saved_views(
    session: AsyncSession,
    *,
    caller_id: UUID,
    filters: SavedViewFilters,
    order_by: SavedViewOrderBy,
    limit: int,
    offset: int,
    deleted_cutoff: datetime,
) -> tuple[list[SavedView], int]:
    """Return one page of the caller's saved views, optionally scoped to one `resource`.

    ``filters.deleted`` swaps the live set for the caller's tombstones still inside
    the restore window. Views being personal data, there is no break-glass — the
    owner scope always applies, which already limits tombstones to the caller's own
    deletes. ``deleted_cutoff`` comes from the route, like
    `get_restorable_saved_view`'s — reaching for the global settings here would let
    the listing and the restore disagree on the window.
    """
    base = (
        SavedView.live_select()
        if not filters.deleted
        else deleted_select(SavedView, deleted_cutoff, deleted_by=caller_id)
    )
    statement = _scope(base, caller_id=caller_id)
    if filters.resource is not None:
        statement = statement.where(col(SavedView.resource) == filters.resource)
    statement = apply_order_by(statement, SavedView, order_by)
    return await paginate(session, statement, limit=limit, offset=offset)


async def create_saved_view(session: AsyncSession, draft: SavedViewCreate, *, caller_id: UUID) -> SavedView:
    """Create a saved view owned by ``caller_id``.

    Raises:
        ConflictError: If the caller already has a live view with the same
            `(resource, name)`.
    """
    view = SavedView(created_by_id=caller_id, resource=draft.resource, name=draft.name, state=draft.state.model_dump())
    session.add(view)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(f"A saved view named {draft.name!r} already exists for {draft.resource}.") from exc
    await session.refresh(view, attribute_names=["created_at", "updated_at"])
    return view


async def update_saved_view(session: AsyncSession, view: SavedView, changes: SavedViewUpdateChanges) -> SavedView:
    """Apply ``changes`` to ``view`` — writes only fields in `changes.model_fields_set`.

    `name` and `state` are editable (`state` is replaced wholesale); `resource`
    is immutable.

    Raises:
        ConflictError: If a renamed view collides with another of the caller's
            views for the same `resource`.
    """
    for field in changes.model_fields_set:
        setattr(view, field, getattr(changes, field))
    session.add(view)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Use the payload value, not `view.*`: a failed flush expires the
        # instance, so touching its attributes would reload on a poisoned session.
        raise ConflictError(f"A saved view named {changes.name!r} already exists for this resource.") from exc
    await session.refresh(view, attribute_names=["updated_at"])
    return view


async def soft_delete_saved_view(session: AsyncSession, view: SavedView, *, by_id: UUID) -> SavedView:
    """Soft-delete ``view`` by stamping `deleted_at`; subsequent reads exclude it."""
    view.soft_delete(by_id)
    session.add(view)
    await session.flush()
    await session.refresh(view)
    return view


async def get_restorable_saved_view(
    session: AsyncSession, view_id: UUID, *, caller_id: UUID, deleted_cutoff: datetime
) -> SavedView:
    """Fetch the tombstoned view ``view_id`` the caller may restore.

    The owner scope of `get_saved_view` over the restorable tombstones.

    Raises:
        NotFoundError: Outside the restore window, never deleted, or not the caller's.
    """
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = (
        _scope(
            deleted_select(SavedView, deleted_cutoff, deleted_by=caller_id).where(col(SavedView.id) == view_id),
            caller_id=caller_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    view = (await session.execute(statement)).scalar_one_or_none()
    if view is None:
        raise NotFoundError(f"No restorable saved view {view_id} was deleted within the restore window.")
    return view


async def restore_saved_view(session: AsyncSession, view: SavedView) -> SavedView:
    """Clear ``view``'s tombstone.

    Raises:
        ConflictError: If the caller has since created a live view with the same
            `(resource, name)` — the partial unique index only covers live rows,
            so the name was free to reuse while this one was tombstoned.
    """
    await restore_row(
        session,
        view,
        conflict_message="A live saved view already uses this name for the resource; rename it and retry.",
    )
    await session.refresh(view)
    return view
