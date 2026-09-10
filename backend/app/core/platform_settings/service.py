"""Accessor and updater for the platform-settings singleton (django-solo analogue).

`get_platform_settings` returns the persisted row, or a **transient** instance carrying the
shipped defaults when none exists — `get_db` doesn't commit read handlers, so lazily inserting
on a GET would only roll back. The row materializes on the first admin write
(`update_platform_settings`, under `@transactional`).

No process-local cache: it couldn't be invalidated across worker processes after a PATCH
elsewhere. The cost is one indexed PK lookup per request (django-solo does the same).
"""

from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_model import BaseModel
from app.core.config import get_settings
from app.core.licenses.catalog import curated_license_id
from app.core.platform_settings.models import PLATFORM_SETTINGS_ID
from app.core.platform_settings.models import PlatformSettings

# Every model field that isn't BaseModel bookkeeping is a knob — derived, so a knob added to
# the model can't be forgotten in the merge base below (silently materializing at its DDL
# default on a sibling's first write) or in the route's audit snapshot.
SETTINGS_KNOBS: tuple[str, ...] = tuple(
    name for name in PlatformSettings.model_fields if name not in BaseModel.model_fields
)


async def get_platform_settings(session: AsyncSession) -> PlatformSettings:
    """Return the singleton row, or a transient instance seeded from the env default.

    The transient instance (no timestamps) signals "never overridden". The singleton has no
    soft-delete, so the row when present is authoritative — no `is_deleted` special-casing.
    """
    row = await session.get(PlatformSettings, PLATFORM_SETTINGS_ID)
    if row is not None:
        return row
    # The transient default points at the curated row for the env SPDX (created by `sync_licenses`,
    # which shares the same deterministic id) — never persisted here (a GET doesn't commit).
    settings = get_settings()
    return PlatformSettings(
        id=PLATFORM_SETTINGS_ID,
        default_license_id=curated_license_id(settings.platform_default_data_license),
        email_verification_ttl_hours=settings.email_verification_ttl_hours,
    )


async def update_platform_settings(session: AsyncSession, **changes: UUID | bool | int) -> PlatformSettings:
    """Apply `changes` (field → new value) to the singleton, materializing the row on first write.

    Atomic `INSERT ... ON CONFLICT DO UPDATE` on the fixed sentinel id: the INSERT arm carries
    every knob at its current effective value merged with `changes` (a first write of one knob
    materializes the others at their effective values, not DDL defaults), the DO UPDATE arm
    writes only `changes` (concurrent PATCHes of different knobs can't clobber each other).
    `updated_at` is set explicitly — a Core upsert bypasses the ORM `onupdate`; the `refresh`
    re-reads an identity-map-pinned instance that `RETURNING` won't overwrite. Does not commit —
    the caller owns the boundary and validates licence refs first (`validate_license_ref`).
    """
    current = await get_platform_settings(session)
    values = {"id": PLATFORM_SETTINGS_ID} | {knob: getattr(current, knob) for knob in SETTINGS_KNOBS} | changes
    statement = (
        pg_insert(PlatformSettings)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["id"],
            set_=dict(changes) | {"updated_at": func.now()},
        )
        .returning(PlatformSettings)
    )
    row = (await session.execute(statement)).scalar_one()
    await session.refresh(row)
    return row
