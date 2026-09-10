"""Integration tests for the notifications service — owner-scoped reads + bulk mark + create."""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.services.users import create_user
from app.core.exceptions import NotFoundError
from app.core.notifications.enums import NotificationObjectType
from app.core.notifications.filters import NotificationFilters
from app.core.notifications.services.notifications import create_notification
from app.core.notifications.services.notifications import get_notification
from app.core.notifications.services.notifications import list_notifications
from app.core.notifications.services.notifications import mark_notifications

pytestmark = pytest.mark.integration


async def _user(db: AsyncSession, email: str) -> User:
    role = Role(name=f"role-{uuid4().hex[:8]}", description="test", permissions=[])
    db.add(role)
    await db.flush()
    return await create_user(db, email=email, roles=[role])


async def test_create_notification_starts_unread(db_session: AsyncSession) -> None:
    user = await _user(db_session, "a@example.com")

    created = await create_notification(
        db_session,
        user_id=user.id,
        name="Evaluation approved",
        description="Your evaluation was approved.",
        object_type=NotificationObjectType.EVALUATION,
        object_id=user.id,
    )

    assert created.read_at is None
    assert created.object_type == NotificationObjectType.EVALUATION
    assert created.created_at is not None


async def test_get_notification_scopes_to_owner(db_session: AsyncSession) -> None:
    owner = await _user(db_session, "owner@example.com")
    other = await _user(db_session, "other@example.com")
    created = await create_notification(db_session, user_id=owner.id, name="Hi")

    assert (await get_notification(db_session, created.id, user_id=owner.id)).id == created.id
    with pytest.raises(NotFoundError):
        await get_notification(db_session, created.id, user_id=other.id)


async def test_list_scopes_and_filters_by_read(db_session: AsyncSession) -> None:
    user = await _user(db_session, "a@example.com")
    other = await _user(db_session, "b@example.com")
    unread = await create_notification(db_session, user_id=user.id, name="unread")
    read = await create_notification(db_session, user_id=user.id, name="read")
    await create_notification(db_session, user_id=other.id, name="theirs")
    await mark_notifications(db_session, user_id=user.id, ids=[read.id], read=True)

    all_items, total = await list_notifications(
        db_session, user_id=user.id, filters=NotificationFilters(), order_by="-created_at", limit=20, offset=0
    )
    assert total == 2
    assert {n.id for n in all_items} == {unread.id, read.id}

    unread_items, unread_total = await list_notifications(
        db_session, user_id=user.id, filters=NotificationFilters(read=False), order_by="-created_at", limit=20, offset=0
    )
    assert unread_total == 1
    assert [n.id for n in unread_items] == [unread.id]


async def test_mark_all_when_ids_empty(db_session: AsyncSession) -> None:
    user = await _user(db_session, "a@example.com")
    first = await create_notification(db_session, user_id=user.id, name="one")
    second = await create_notification(db_session, user_id=user.id, name="two")

    updated = await mark_notifications(db_session, user_id=user.id, ids=[], read=True)

    assert updated == 2
    assert (await get_notification(db_session, first.id, user_id=user.id)).read_at is not None
    assert (await get_notification(db_session, second.id, user_id=user.id)).read_at is not None


async def test_mark_is_idempotent_and_counts_only_changes(db_session: AsyncSession) -> None:
    user = await _user(db_session, "a@example.com")
    note = await create_notification(db_session, user_id=user.id, name="one")

    assert await mark_notifications(db_session, user_id=user.id, ids=[note.id], read=True) == 1
    # Already read: no row flips, count is 0, timestamp preserved.
    original = (await get_notification(db_session, note.id, user_id=user.id)).read_at
    assert await mark_notifications(db_session, user_id=user.id, ids=[note.id], read=True) == 0
    assert (await get_notification(db_session, note.id, user_id=user.id)).read_at == original


async def test_mark_unread_clears_read_at(db_session: AsyncSession) -> None:
    user = await _user(db_session, "a@example.com")
    note = await create_notification(db_session, user_id=user.id, name="one")
    await mark_notifications(db_session, user_id=user.id, ids=[note.id], read=True)

    updated = await mark_notifications(db_session, user_id=user.id, ids=[note.id], read=False)

    assert updated == 1
    assert (await get_notification(db_session, note.id, user_id=user.id)).read_at is None


async def test_mark_does_not_touch_other_users(db_session: AsyncSession) -> None:
    user = await _user(db_session, "a@example.com")
    other = await _user(db_session, "b@example.com")
    theirs = await create_notification(db_session, user_id=other.id, name="theirs")

    updated = await mark_notifications(db_session, user_id=user.id, ids=[], read=True)

    assert updated == 0
    assert (await get_notification(db_session, theirs.id, user_id=other.id)).read_at is None


async def test_mark_ignores_foreign_and_unknown_ids(db_session: AsyncSession) -> None:
    user = await _user(db_session, "a@example.com")
    other = await _user(db_session, "b@example.com")
    theirs = await create_notification(db_session, user_id=other.id, name="theirs")

    updated = await mark_notifications(db_session, user_id=user.id, ids=[theirs.id, uuid4()], read=True)

    assert updated == 0
    assert (await get_notification(db_session, theirs.id, user_id=other.id)).read_at is None
