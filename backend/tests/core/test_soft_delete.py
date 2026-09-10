"""Soft-delete helpers — `BaseModel.live_*` factories and `with_live` option.

Integration tests run against the real Postgres test DB (savepoint-isolated)
so the criteria reaches the actual SQL — bulk UPDATE/DELETE behavior under
`with_loader_criteria` is the kind of thing mocks would silently hide.
"""

from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserRole
from app.core.soft_delete import with_live


@pytest.mark.unit
def test_is_deleted_reflects_deleted_at() -> None:
    user = User(email="probe@example.com")

    assert user.is_deleted is False

    user.soft_delete(None)
    assert user.is_deleted is True

    user.restore()
    assert user.is_deleted is False


@pytest.mark.unit
def test_soft_delete_sets_utc_timestamp() -> None:
    user = User(email="probe@example.com")

    user.soft_delete(None)

    assert user.deleted_at is not None
    assert user.deleted_at.tzinfo is not None


@pytest.mark.unit
def test_soft_delete_records_the_actor() -> None:
    # `deleted_by_id` is what scopes a later restore to the person who deleted the
    # row, so the actor has to survive the round trip — and vanish on restore, or a
    # re-deleted row would still name the first deleter.
    actor_id = uuid4()
    user = User(email="probe@example.com")

    user.soft_delete(actor_id)
    assert user.deleted_by_id == actor_id

    user.restore()
    assert user.deleted_by_id is None


@pytest.mark.unit
def test_soft_delete_without_an_actor_is_system_initiated() -> None:
    # The reapers (media, exports) pass `None` deliberately: nobody deleted the row,
    # so nobody but a break-glass caller may restore it.
    user = User(email="probe@example.com")

    user.soft_delete(None)

    assert user.deleted_at is not None
    assert user.deleted_by_id is None


@pytest.mark.integration
async def test_bulk_soft_delete_records_the_actor(db_session: AsyncSession) -> None:
    # The documented bulk idiom: `live_update().values(deleted_at=..., deleted_by_id=...)`.
    # Stamping only `deleted_at` would produce rows no owner can ever restore.
    actor_id = uuid4()
    user = User(email="bulk@example.com")
    db_session.add(user)
    await db_session.flush()

    await db_session.execute(User.live_update().values(deleted_at=func.now(), deleted_by_id=actor_id))
    await db_session.flush()

    row = (await db_session.execute(select(User.deleted_at, User.deleted_by_id))).one()  # ty: ignore[no-matching-overload]
    assert row.deleted_at is not None
    assert row.deleted_by_id == actor_id


@pytest.mark.integration
async def test_live_select_excludes_soft_deleted_rows(db_session: AsyncSession) -> None:
    alive = User(email="alive@example.com")
    deleted = User(email="deleted@example.com")
    deleted.soft_delete(None)
    db_session.add_all([alive, deleted])
    await db_session.flush()

    users = (await db_session.execute(User.live_select())).scalars().all()
    emails = {u.email for u in users}

    assert "alive@example.com" in emails
    assert "deleted@example.com" not in emails


@pytest.mark.integration
async def test_plain_select_includes_soft_deleted_rows(db_session: AsyncSession) -> None:
    # Guards against any future reintroduction of a global listener that
    # would silently filter unwrapped statements.
    deleted = User(email="deleted@example.com")
    deleted.soft_delete(None)
    db_session.add(deleted)
    await db_session.flush()

    emails = (await db_session.execute(select(User.email))).scalars().all()  # ty: ignore[no-matching-overload]

    assert "deleted@example.com" in emails


@pytest.mark.integration
async def test_restore_brings_row_back_into_live_select(db_session: AsyncSession) -> None:
    user = User(email="ghost@example.com")
    user.soft_delete(None)
    db_session.add(user)
    await db_session.flush()

    user.restore()
    await db_session.flush()

    users = (await db_session.execute(User.live_select())).scalars().all()
    assert "ghost@example.com" in {u.email for u in users}


@pytest.mark.integration
async def test_email_reusable_after_soft_delete(db_session: AsyncSession) -> None:
    first = User(email="reuse@example.com")
    first.soft_delete(None)
    db_session.add(first)
    await db_session.flush()

    second = User(email="reuse@example.com")
    db_session.add(second)
    await db_session.flush()

    live = (await db_session.execute(User.live_select().where(col(User.email) == "reuse@example.com"))).scalar_one()
    assert live.id == second.id


@pytest.mark.integration
async def test_live_update_skips_soft_deleted_rows(db_session: AsyncSession) -> None:
    alive = User(email="alive@example.com", first_name="orig")
    dead = User(email="dead@example.com", first_name="orig")
    dead.soft_delete(None)
    db_session.add_all([alive, dead])
    await db_session.flush()

    await db_session.execute(User.live_update().values(first_name="bumped"))
    await db_session.flush()

    rows = (await db_session.execute(select(User.email, User.first_name))).all()  # ty: ignore[no-matching-overload]
    by_email = {row.email: row.first_name for row in rows}

    assert by_email["alive@example.com"] == "bumped"
    assert by_email["dead@example.com"] == "orig"


@pytest.mark.integration
async def test_plain_bulk_update_touches_soft_deleted_rows(db_session: AsyncSession) -> None:
    # Plain `update(User)` MUST NOT filter — that's the contract. Use
    # `User.live_update()` for live-only writes.
    dead = User(email="dead@example.com", first_name="orig")
    dead.soft_delete(None)
    db_session.add(dead)
    await db_session.flush()

    await db_session.execute(update(User).values(first_name="bumped"))
    await db_session.flush()

    [first_name] = (
        (
            await db_session.execute(select(User.first_name).where(User.email == "dead@example.com"))  # ty: ignore[no-matching-overload]
        )
        .scalars()
        .all()
    )
    assert first_name == "bumped"


@pytest.mark.integration
async def test_live_update_with_deleted_at_skips_already_tombstoned(db_session: AsyncSession) -> None:
    # Bulk soft-delete via `live_update().values(deleted_at=func.now())`: the
    # live filter excludes already-tombstoned rows so their original timestamp
    # survives the statement.
    alive = User(email="alive@example.com")
    already_dead = User(email="dead@example.com")
    already_dead.soft_delete(None)
    original_deleted_at = already_dead.deleted_at
    db_session.add_all([alive, already_dead])
    await db_session.flush()

    await db_session.execute(User.live_update().values(deleted_at=func.now()))
    await db_session.flush()

    rows = (await db_session.execute(select(User.email, User.deleted_at))).all()  # ty: ignore[no-matching-overload]
    by_email = {row.email: row.deleted_at for row in rows}

    assert by_email["alive@example.com"] is not None
    assert by_email["dead@example.com"] == original_deleted_at


@pytest.mark.integration
async def test_live_update_with_where_targets_subset(db_session: AsyncSession) -> None:
    keep = User(email="keep@example.com")
    drop = User(email="drop@example.com")
    db_session.add_all([keep, drop])
    await db_session.flush()

    await db_session.execute(
        User.live_update().values(deleted_at=func.now()).where(col(User.email) == "drop@example.com")
    )
    await db_session.flush()

    rows = (await db_session.execute(select(User.email, User.deleted_at))).all()  # ty: ignore[no-matching-overload]
    by_email = {row.email: row.deleted_at for row in rows}

    assert by_email["keep@example.com"] is None
    assert by_email["drop@example.com"] is not None


@pytest.mark.integration
async def test_plain_bulk_delete_touches_soft_deleted_rows(db_session: AsyncSession) -> None:
    dead = User(email="dead@example.com")
    dead.soft_delete(None)
    db_session.add(dead)
    await db_session.flush()

    await db_session.execute(delete(User))
    await db_session.flush()

    surviving = (await db_session.execute(select(User.email))).scalars().all()  # ty: ignore[no-matching-overload]
    assert "dead@example.com" not in surviving


@pytest.mark.integration
async def test_with_live_filters_eager_loaded_relationship(db_session: AsyncSession) -> None:
    user = User(email="rel@example.com")
    alive_role = Role(name="alive-role")
    dead_role = Role(name="dead-role")
    db_session.add_all([user, alive_role, dead_role])
    await db_session.flush()
    db_session.add_all(
        [
            UserRole(user_id=user.id, role_id=alive_role.id),
            UserRole(user_id=user.id, role_id=dead_role.id),
        ]
    )
    await db_session.flush()
    dead_role.soft_delete(None)
    await db_session.flush()

    loaded = (
        await db_session.execute(
            User.live_select()
            .options(
                selectinload(User.roles),  # ty: ignore[invalid-argument-type]
                with_live(Role),
            )
            .where(col(User.id) == user.id)
        )
    ).scalar_one()

    role_names = {r.name for r in loaded.roles}
    assert role_names == {"alive-role"}


@pytest.mark.integration
async def test_eager_load_without_with_live_includes_soft_deleted_children(db_session: AsyncSession) -> None:
    # Documents the explicit-per-call contract: `User.live_select()` only
    # attaches criteria for `User`. Forgetting `with_live(Role)` leaks
    # tombstoned children — by design — instead of silently filtering them
    # via a global subclass walk.
    user = User(email="rel@example.com")
    dead_role = Role(name="dead-role")
    db_session.add_all([user, dead_role])
    await db_session.flush()
    db_session.add(UserRole(user_id=user.id, role_id=dead_role.id))
    await db_session.flush()
    dead_role.soft_delete(None)
    await db_session.flush()

    loaded = (
        await db_session.execute(
            User.live_select()
            .options(selectinload(User.roles))  # ty: ignore[invalid-argument-type]
            .where(col(User.id) == user.id)
        )
    ).scalar_one()

    assert {r.name for r in loaded.roles} == {"dead-role"}
