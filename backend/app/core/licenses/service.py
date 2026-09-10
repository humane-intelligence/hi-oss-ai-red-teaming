"""Data-license CRUD plus the curated-catalog sync.

The platform-settings singleton lives in `app.core.platform_settings`; this service reads it
only to resolve the current default row (`get_default_license`) and to guard deleting the
current default (`soft_delete_data_license`).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.licenses.catalog import CURATED_LICENSES
from app.core.licenses.catalog import curated_license_id
from app.core.licenses.catalog import curated_ships_content
from app.core.licenses.models import DataLicense
from app.core.licenses.models import defer_license_text
from app.core.licenses.schemas import LicenseUpdate
from app.core.pagination import paginate
from app.core.platform_settings.service import get_platform_settings
from app.core.restore import deleted_select
from app.core.restore import restore_row


async def get_default_license(session: AsyncSession) -> DataLicense:
    """The platform default licence row (resolved from the singleton's `default_license_id`).

    Loaded by PK regardless of `deleted_at` — the default is a curated row `sync_licenses` keeps
    live, and lineage doesn't lapse. The one-per-request platform-default projections pass this row
    into the response `from_model`s, so the effective-license cascade resolves on rows in-memory.
    """
    settings = await get_platform_settings(session)
    # `content` deferred like every other projection path: the platform default is the row most
    # requests resolve, and none of them render its text.
    lic = await session.get(DataLicense, settings.default_license_id, options=[defer_license_text()])
    if lic is None:  # invariant: the default always resolves — `sync_licenses` keeps the curated default live
        raise RuntimeError(f"Platform default license {settings.default_license_id} is missing; run synclicenses.")
    return lic


async def list_data_licenses(
    session: AsyncSession,
    *,
    name: str | None = None,
    deleted: bool = False,
    authored_by: UUID | None = None,
    limit: int,
    offset: int,
    deleted_cutoff: datetime,
) -> tuple[list[DataLicense], int]:
    """Return one page of live licences (curated + user-authored), ordered by name then id.

    ``deleted`` swaps in the tombstones still inside the restore window, most-recently
    deleted first (this list takes no client ordering).

    ``authored_by`` narrows those to that user's **own** licences — deliberately the
    author, not the deleter, so the list matches exactly what `restore_data_license`
    accepts. An admin deleting someone else's licence stamps `deleted_by_id` with their
    own id; scoping on that would hide the row from the one person still allowed to
    restore it. The route passes the caller unless they hold `licenses:manage`, which
    lifts the scope just as it lifts the restore gate. Curated tombstones can appear
    here (nothing stops one being deleted straight in the DB) but are never restorable
    through the API; a resync revives them.
    ``deleted_cutoff`` comes from the route, like `get_restorable_data_license`'s — reaching for
    the global settings here would let the listing and the restore disagree on the window.
    """
    if deleted:
        statement = deleted_select(DataLicense, deleted_cutoff, deleted_by=None)
        if authored_by is not None:
            statement = statement.where(col(DataLicense.created_by_id) == authored_by)
    else:
        statement = DataLicense.live_select()
    # The list projection has no `content` field, so a 100-row page would ship every legal text for
    # nothing; the flag it does project rides on the mapped `DataLicense.has_text` expression.
    statement = statement.options(defer_license_text())
    if name:
        statement = statement.where(col(DataLicense.name).ilike(f"%{name}%", escape="\\"))
    if deleted:
        statement = statement.order_by(col(DataLicense.deleted_at).desc(), col(DataLicense.id))
    else:
        statement = statement.order_by(col(DataLicense.name), col(DataLicense.id))
    return await paginate(session, statement, limit=limit, offset=offset)


async def get_data_license(
    session: AsyncSession, license_id: UUID, *, for_update: bool = False, include_deleted: bool = False
) -> DataLicense:
    """Fetch one licence by id, or raise `NotFoundError`.

    Live-only unless `include_deleted`, which the read path sets and no write path does: an
    evaluation or group keeps resolving a licence after its delete (lineage doesn't lapse), so the
    text behind that reference stays readable — while the edit and delete paths still read a
    tombstone as missing. No restore window either, unlike `get_restorable_data_license`: the
    reference outlives it.
    """
    statement = (select(DataLicense) if include_deleted else DataLicense.live_select()).where(
        col(DataLicense.id) == license_id
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"Data license {license_id} not found.")
    return row


async def _license_is_live(session: AsyncSession, license_id: UUID) -> bool:
    return await session.scalar(DataLicense.live_select().where(col(DataLicense.id) == license_id).limit(1)) is not None


async def validate_license_ref(session: AsyncSession, license_id: UUID) -> None:
    """Raise `BadRequestError` unless `license_id` references a live licence (write-path guard)."""
    if not await _license_is_live(session, license_id):
        raise BadRequestError(f"Unknown data license id '{license_id}'.")


async def assert_curated_license_synced(session: AsyncSession, license_id: UUID) -> None:
    """Raise `RuntimeError` unless a licence the server itself derived is present.

    Mirrors `get_default_license`: a missing curated row means `synclicenses` has not run on this
    environment, which is a server fault. Routed away from `validate_license_ref` so it does not
    surface as a 400 blaming the caller for an id they never sent — a 4xx is also invisible to error
    reporting, leaving the operator one misleading message and no trace.
    """
    if not await _license_is_live(session, license_id):
        raise RuntimeError(f"Curated license {license_id} is missing; run synclicenses.")


# The only field of a curated licence the API may write, and only on a row whose text the catalog
# leaves empty: there a resync has nothing to revert with (see `sync_licenses`, which drops `content`
# from the upsert for those entries). Where the catalog does ship text, code owns it — see
# `curated_ships_content`.
CURATED_EDITABLE_FIELDS = frozenset({"content"})


def _assert_may_update_license(lic: DataLicense, changes: LicenseUpdate, *, caller_id: UUID, can_manage: bool) -> None:
    """Gate an edit, by row kind.

    A **curated** licence takes a `content`-only edit from a `licenses:manage` holder, and only where
    the catalog ships no text for it: the platform has to be able to put the legal text of a shipped
    licence somewhere, and for those entries `sync_licenses` leaves the column alone. An entry that
    carries its own text is refused outright — accepting it would answer 200 to a write the next
    resync reverts. Every other field stays code-owned. A **user-authored** licence follows the
    ownership rule in `_assert_may_manage_license`.

    The checks run widest-cause-first, so the answer names what actually stopped the write: a body
    that sets nothing, then a field no caller may write, then a row whose text code owns, then the
    permission. Ordering the permission earlier would tell a caller without `licenses:manage` that
    the grant unlocks a rename, which it does not.

    Raises:
        BadRequestError: a curated licence patched with an empty body.
        ForbiddenError: the caller may not apply these changes to this licence.
    """
    if lic.created_by_id is not None:
        _assert_may_manage_license(lic, caller_id=caller_id, can_manage=can_manage)
        return
    if not changes.model_fields_set:
        # `content` is the only editable field of a curated row, so a body that sets nothing cannot
        # be the no-op a PATCH is elsewhere — it asks for something this row does not offer.
        raise BadRequestError("Nothing to update; 'content' is the only editable field of a curated license.")
    code_owned = sorted(changes.model_fields_set - CURATED_EDITABLE_FIELDS)
    if code_owned:
        raise ForbiddenError(
            f"Curated licenses are managed in code; only 'content' is editable (not {', '.join(code_owned)})."
        )
    if curated_ships_content(lic.id):
        raise ForbiddenError(
            "This license's text is shipped in the catalog; editing it here would be reverted by the "
            "next resync. Change it in the catalog instead."
        )
    if not can_manage:
        raise ForbiddenError("Only licenses:manage holders may edit a curated license's text.")


def _assert_may_manage_license(lic: DataLicense, *, caller_id: UUID, can_manage: bool) -> None:
    """Gate an edit/delete of a user-authored licence.

    A curated licence (`created_by_id is None`) is reference data managed in code
    (`catalog.py` + `sync_licenses`): deleting or restoring one is refused for everyone, including
    `licenses:manage` holders, since a resync would revive it anyway. Editing is the one exception
    and does not come through here — see `_assert_may_update_license`, which takes a `content`-only
    change from a manager. A user-authored licence is editable only by its author unless the caller
    can manage.

    Raises:
        ForbiddenError: the caller may not manage this licence.
    """
    if lic.created_by_id is None:
        raise ForbiddenError("Curated licenses are managed in code and cannot be deleted or restored via the API.")
    if can_manage:
        return
    if lic.created_by_id != caller_id:
        raise ForbiddenError("You may only edit licenses you created.")


async def create_data_license(
    session: AsyncSession,
    *,
    caller_id: UUID,
    name: str,
    version: str | None,
    short_description: str,
    content: str,
    reference_url: str | None,
    protects_conversation_data: bool = False,
) -> DataLicense:
    """Create a user-authored licence owned by `caller_id` (no `spdx_id` — curated only)."""
    lic = DataLicense(
        name=name,
        version=version,
        short_description=short_description,
        content=content,
        reference_url=reference_url,
        protects_conversation_data=protects_conversation_data,
        created_by_id=caller_id,
    )
    session.add(lic)
    await session.flush()
    # `has_text` is a mapper expression: unloaded on a just-inserted row, so the projection would
    # reach for it lazily and fail under async. Same targeted refresh as the timestamps.
    await session.refresh(lic, attribute_names=["created_at", "updated_at", "has_text"])
    return lic


async def update_data_license(
    session: AsyncSession, lic: DataLicense, changes: LicenseUpdate, *, caller_id: UUID, can_manage: bool
) -> DataLicense:
    """Apply `changes` (omitted fields untouched), gated by `_assert_may_update_license`."""
    _assert_may_update_license(lic, changes, caller_id=caller_id, can_manage=can_manage)
    for field in changes.model_fields_set:
        setattr(lic, field, getattr(changes, field))
    session.add(lic)
    await session.flush()
    # `has_text` too: an edit of `content` leaves the mapped flag stale.
    await session.refresh(lic, attribute_names=["updated_at", "has_text"])
    return lic


async def soft_delete_data_license(
    session: AsyncSession, lic: DataLicense, *, caller_id: UUID, can_manage: bool
) -> None:
    """Soft-delete a licence; refuses (409) to delete the platform default.

    Referenced groups/evaluations keep resolving the tombstoned licence — its lineage doesn't
    lapse (mirroring the cascade's live-parent handling); it simply leaves the picker.

    The curated-managed check runs first, so the 409 default-guard is only reachable for a
    **user-authored** default: the shipped default is curated, so deleting the *current* default
    when it is the shipped one is refused earlier with 403 (curated, managed in code), not 409.

    Raises:
        ForbiddenError: the caller may not manage this licence (curated, or another user's).
        ConflictError: the licence is a user-authored current platform default.
    """
    _assert_may_manage_license(lic, caller_id=caller_id, can_manage=can_manage)
    default_id = (await get_platform_settings(session)).default_license_id
    if lic.id == default_id:
        raise ConflictError("Cannot delete the platform default license; set another default first.")
    lic.soft_delete(caller_id)
    session.add(lic)
    await session.flush()


async def get_restorable_data_license(
    session: AsyncSession, license_id: UUID, *, deleted_cutoff: datetime
) -> DataLicense:
    """Fetch the tombstoned licence ``license_id`` inside the restore window.

    No ownership predicate here: `restore_data_license` applies the same
    curated/author gate the edit and delete paths use, so the 404-vs-403 split matches
    them (an unknown id is missing; another owner's licence is forbidden).

    Raises:
        NotFoundError: Never deleted, or deleted longer than the window ago.
    """
    # `populate_existing` refreshes a cached instance from the locked read — see
    # `get_note` for why the lock is otherwise toothless.
    statement = (
        deleted_select(DataLicense, deleted_cutoff, deleted_by=None)
        .where(col(DataLicense.id) == license_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    lic = (await session.execute(statement)).scalar_one_or_none()
    if lic is None:
        raise NotFoundError(f"No restorable data license {license_id} was deleted within the restore window.")
    return lic


async def restore_data_license(
    session: AsyncSession, lic: DataLicense, *, caller_id: UUID, can_manage: bool
) -> DataLicense:
    """Clear ``lic``'s tombstone, gated exactly like a delete of it.

    A **curated** tombstone is refused (403): curated rows are reference data owned by
    `catalog.py`, and `sync_licenses` already revives one on the next resync — letting
    the API restore it would fork ownership of a row a deploy step rewrites. A
    user-authored one is restorable by its author, or by a `licenses:manage` holder.

    Restoring does not touch the platform-default pointer: the delete refused to
    remove the current default, so a tombstone was never the default.

    No uniqueness pre-check, unlike the other restores: the only unique key on this table
    is the partial index on `spdx_id`, and `spdx_id` is never API-writable (`sync_licenses`
    alone sets it, on curated rows) — so every row that gets past the curated gate above
    carries NULL. `restore_row`'s `IntegrityError` backstop remains the guard if that
    ever changes.

    Raises:
        ForbiddenError: Curated, or another owner's licence without `licenses:manage`.
    """
    _assert_may_manage_license(lic, caller_id=caller_id, can_manage=can_manage)
    await restore_row(session, lic, conflict_message="Data license cannot be restored.")
    await session.refresh(lic)
    return lic


async def sync_licenses(session: AsyncSession) -> int:
    """Idempotently upsert the curated catalog into `data_licenses`, returning the row count.

    Keyed by the deterministic `curated_license_id`, so re-runs reconcile the catalog metadata +
    content without duplicating rows. An entry with `publishes_spdx=False` stores no `spdx_id`, and one
    carrying no `content` leaves the row's text untouched (the API may fill it in).
    Curated rows always carry `created_by_id = NULL` and `deleted_at = NULL` (a resync revives a
    tombstoned curated row).
    Mirrors `syncroles`: a deploy step after `migrate` and part of `seedlocal`.
    """
    for lic in CURATED_LICENSES:
        values = {
            "id": curated_license_id(lic.spdx_id),
            "name": lic.name,
            "version": lic.version,
            "short_description": lic.short_description,
            "content": lic.content,
            "reference_url": lic.reference_url,
            "spdx_id": lic.spdx_id if lic.publishes_spdx else None,
            "protects_conversation_data": lic.protects_conversation_data,
            "created_by_id": None,
        }
        updates = {k: v for k, v in values.items() if k != "id"}
        if not lic.content:
            # An entry shipping no text leaves the row's `content` alone, so a licence text added
            # through the API survives the next resync.
            del updates["content"]
        await session.execute(
            pg_insert(DataLicense)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["id"],
                # Clear both tombstone columns, as `BaseModel.restore` does — a revived
                # curated row must not keep the actor who deleted it.
                set_=updates | {"deleted_at": None, "deleted_by_id": None, "updated_at": func.now()},
            )
        )
    await session.flush()
    return len(CURATED_LICENSES)
