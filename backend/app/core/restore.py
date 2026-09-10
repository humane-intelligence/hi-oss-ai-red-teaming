"""Reading tombstones back — the inverse of [soft_delete.py](soft_delete.py).

`BaseModel.live_select` hides soft-deleted rows; these helpers surface the ones
still inside the restore window (`Settings.restore_window_days`) so a list
endpoint can offer a "deleted" view and a restore endpoint can revive one row.

Two scoping rules, both enforced here rather than per entity:

* **Window** — a tombstone older than the window is not addressable (404). The
  row is never purged, it just stops being restorable.
* **Deleter** — pass `deleted_by` to see and restore only that actor's own
  deletes (a participant); pass `None` for the break-glass/admin caller who
  reaches every tombstone, or for an entity scoped by something else entirely
  (a domain permission, an object role). Required either way: the permissive
  branch is never what you get by forgetting the argument. Rows predating
  `deleted_by_id` carry NULL, so they fall out as admin-only — and for an entity
  with no break-glass (saved views) they are unrestorable by anyone. Accepted as
  a one-window blind spot at rollout: it clears itself once every pre-migration
  tombstone ages past the window.

`deleted_select` deliberately omits `with_live(model)` — filtering the target's
tombstones away is exactly what it must not do. A statement that eager-loads a
*related* soft-deletable model still needs `with_live(OtherModel)` among its
`.options(...)`; a many-to-one being projected into a response needs the
`BaseModel.live` guard at the call site instead, which is a different tool for a
different job (see its docstring for why the loader option isn't enough there).
"""

import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Annotated

from fastapi import Query
from sqlalchemy import Select
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.roles import Permission
from app.core.base_model import BaseModel
from app.core.config import Settings
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError

if TYPE_CHECKING:
    from app.core.auth.schemas import SessionUser

# Two axes, composed below into the combinations that exist: who the listing scopes to, and
# whether the endpoint takes an `order_by` the flag must not silently override. The window's
# length is deliberately absent from every one: it is server-side config an operator can retune
# without the published contract going stale (see RESTORE_WINDOW_DAYS in the README).
_WINDOW_AND_SCOPE = (
    "List soft-deleted items instead of live ones. Bounded by the platform restore window; a caller "
    "without the break-glass sees only the items they deleted themselves."
)

# For a resource with no admin break-glass, where the sentence above would advertise a tier
# that cannot exist — the owner scope is the only scope there is.
_WINDOW_AND_OWNER = (
    "List your soft-deleted items instead of the live ones. Bounded by the platform restore window, "
    "and always limited to items you deleted yourself — this resource has no admin break-glass."
)

# The mirror image: an admin-only resource where the delete permission gates the whole tombstone
# view and there is no owner tier under it, so every holder sees every actor's deletes. Saying
# "only the items they deleted themselves" here would under-promise what the listing returns.
_WINDOW_AND_PERMISSION = (
    "List soft-deleted items instead of live ones. Bounded by the platform restore window; requires the "
    "same permission as deleting, and shows every actor's deletes — there is no per-deleter narrowing."
)

# And the fourth scope: the row's *author* rather than whoever deleted it. An admin's delete of
# someone else's row leaves the author as the one allowed to restore it, so scoping that listing
# by deleter would hide the row from the only caller who can act on it.
_WINDOW_AND_AUTHOR = (
    "List soft-deleted items instead of live ones. Bounded by the platform restore window; a caller "
    "without the break-glass sees only the items they authored — an admin's delete of someone else's "
    "item still leaves it here for its author to restore."
)

_ORDER_HINT = "Ordering is unchanged by this flag — pass `order_by=-deleted_at` for most-recently-deleted first."
# For lists that expose no `order_by` at all: there is no client-chosen order to preserve,
# and creation order is the wrong reading for a tombstone list.
_NEWEST_FIRST = "This list has no `order_by` parameter; the deleted view is ordered most-recently-deleted first."

DELETED_FILTER_DESCRIPTION = f"{_WINDOW_AND_SCOPE} {_ORDER_HINT}"
DELETED_FILTER_DESCRIPTION_NEWEST_FIRST = f"{_WINDOW_AND_SCOPE} {_NEWEST_FIRST}"
DELETED_FILTER_DESCRIPTION_OWNER_ONLY = f"{_WINDOW_AND_OWNER} {_ORDER_HINT}"
DELETED_FILTER_DESCRIPTION_ANY_DELETER = f"{_WINDOW_AND_PERMISSION} {_ORDER_HINT}"
DELETED_FILTER_DESCRIPTION_ANY_DELETER_NEWEST_FIRST = f"{_WINDOW_AND_PERMISSION} {_NEWEST_FIRST}"
DELETED_FILTER_DESCRIPTION_AUTHOR_NEWEST_FIRST = f"{_WINDOW_AND_AUTHOR} {_NEWEST_FIRST}"

DeletedFilter = Annotated[bool, Query(description=DELETED_FILTER_DESCRIPTION)]
DeletedFilterNewestFirst = Annotated[bool, Query(description=DELETED_FILTER_DESCRIPTION_NEWEST_FIRST)]
DeletedFilterOwnerOnly = Annotated[bool, Query(description=DELETED_FILTER_DESCRIPTION_OWNER_ONLY)]
DeletedFilterAnyDeleter = Annotated[bool, Query(description=DELETED_FILTER_DESCRIPTION_ANY_DELETER)]
DeletedFilterAnyDeleterNewestFirst = Annotated[
    bool, Query(description=DELETED_FILTER_DESCRIPTION_ANY_DELETER_NEWEST_FIRST)
]
DeletedFilterAuthorNewestFirst = Annotated[bool, Query(description=DELETED_FILTER_DESCRIPTION_AUTHOR_NEWEST_FIRST)]


def assert_may_list_deleted(deleted: bool, caller: SessionUser, permission: Permission, *, entity: str) -> None:
    """403 a `deleted=true` listing unless ``caller`` holds ``permission``.

    The deleted view is a restore surface, so it takes the permission that restores
    rather than the one that reads — one shape for every flat listing's gate.
    """
    if deleted and permission not in caller.permissions:
        raise ForbiddenError(f"Listing deleted {entity} requires the '{permission.value}' permission.")


def restore_cutoff(settings: Settings) -> datetime:
    """Oldest `deleted_at` still restorable."""
    return datetime.now(UTC) - timedelta(days=settings.restore_window_days)


def deleted_select[T: BaseModel](
    model: type[T],
    cutoff: datetime,
    *,
    deleted_by: uuid.UUID | None,
) -> Select[tuple[T]]:
    """`select(model)` narrowed to tombstones inside the restore window.

    ``deleted_by`` has no default: `None` widens the listing to every actor's
    tombstones, which is a decision each caller states rather than inherits.

    Ordering is left to the caller — each list endpoint keeps its own default, so a
    deleted listing needs an explicit `-deleted_at` to read newest-first.
    """
    statement = select(model).where(col(model.deleted_at).is_not(None), col(model.deleted_at) >= cutoff)
    if deleted_by is not None:
        statement = statement.where(col(model.deleted_by_id) == deleted_by)
    return statement


async def restore_row(session: AsyncSession, row: BaseModel, *, conflict_message: str) -> None:
    """Clear `row`'s tombstone, translating a unique-key clash into 409.

    Every soft-delete-aware unique index is partial (`WHERE deleted_at IS NULL`),
    so reviving a row whose key a live row now holds violates it. No caller
    pre-checks: ``conflict_message`` *is* the user-facing text, and this catch is
    what keeps the driver error from surfacing as a 500.
    """
    row.restore()
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise ConflictError(conflict_message) from exc
