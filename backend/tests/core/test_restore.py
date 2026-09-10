"""Restore helpers — window cutoff, tombstone scoping, and the 409 backstop.

Integration tests run against the real Postgres test DB (savepoint-isolated) so
the window/deleter predicates reach actual SQL, and so the partial unique index
that makes `restore_row` raise is the real one.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import User
from app.core.exceptions import ConflictError
from app.core.restore import deleted_select
from app.core.restore import restore_cutoff
from app.core.restore import restore_row
from tests.conftest import make_settings


def _tombstone(user: User, *, days_ago: float, by_id: object = None) -> User:
    user.deleted_at = datetime.now(UTC) - timedelta(days=days_ago)
    user.deleted_by_id = by_id  # ty: ignore[invalid-assignment]
    return user


@pytest.mark.unit
def test_restore_cutoff_trails_now_by_the_configured_window() -> None:
    cutoff = restore_cutoff(make_settings(restore_window_days=7))

    assert timedelta(days=7) - (datetime.now(UTC) - cutoff) < timedelta(seconds=5)


@pytest.mark.integration
async def test_deleted_select_returns_only_tombstones_inside_the_window(db_session: AsyncSession) -> None:
    cutoff = restore_cutoff(make_settings(restore_window_days=7))
    live = User(email="live@example.com")
    fresh = _tombstone(User(email="fresh@example.com"), days_ago=1)
    stale = _tombstone(User(email="stale@example.com"), days_ago=30)
    db_session.add_all([live, fresh, stale])
    await db_session.flush()

    rows = (await db_session.execute(deleted_select(User, cutoff, deleted_by=None))).scalars().all()

    assert {row.email for row in rows} == {"fresh@example.com"}


@pytest.mark.integration
async def test_deleted_select_scopes_to_one_deleter(db_session: AsyncSession) -> None:
    cutoff = restore_cutoff(make_settings(restore_window_days=7))
    actor = uuid4()
    mine = _tombstone(User(email="mine@example.com"), days_ago=1, by_id=actor)
    theirs = _tombstone(User(email="theirs@example.com"), days_ago=1, by_id=uuid4())
    legacy = _tombstone(User(email="legacy@example.com"), days_ago=1)
    db_session.add_all([mine, theirs, legacy])
    await db_session.flush()

    scoped = (await db_session.execute(deleted_select(User, cutoff, deleted_by=actor))).scalars().all()
    unscoped = (await db_session.execute(deleted_select(User, cutoff, deleted_by=None))).scalars().all()

    assert {row.email for row in scoped} == {"mine@example.com"}
    # An explicit `deleted_by=None` is the break-glass view — legacy NULL rows included.
    assert {"mine@example.com", "theirs@example.com", "legacy@example.com"} <= {row.email for row in unscoped}


@pytest.mark.integration
async def test_restore_row_clears_the_tombstone(db_session: AsyncSession) -> None:
    user = _tombstone(User(email="back@example.com"), days_ago=1, by_id=uuid4())
    db_session.add(user)
    await db_session.flush()

    await restore_row(db_session, user, conflict_message="taken")

    assert user.deleted_at is None
    assert user.deleted_by_id is None


@pytest.mark.integration
async def test_restore_row_conflicts_when_a_live_row_holds_the_key(db_session: AsyncSession) -> None:
    # `users.email` is unique among live rows only, so the tombstone's email was
    # free to reuse — reviving it would put two live rows on the same key.
    tombstoned = _tombstone(User(email="reused@example.com"), days_ago=1, by_id=uuid4())
    db_session.add(tombstoned)
    await db_session.flush()
    db_session.add(User(email="reused@example.com"))
    await db_session.flush()

    with pytest.raises(ConflictError):
        await restore_row(db_session, tombstoned, conflict_message="Email is taken by a live user.")
