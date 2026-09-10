"""Integration tests for `app.core.auth.services.users` — service layer over a real DB."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.filters import UserFilters
from app.core.auth.models import ProviderIdentity
from app.core.auth.models import Role
from app.core.auth.models import SettableUserStatus
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.auth.schemas import UserResponse
from app.core.auth.services.users import UserUpdateChanges
from app.core.auth.services.users import assert_can_assign_roles
from app.core.auth.services.users import assert_can_delete_user
from app.core.auth.services.users import assert_can_manage_status
from app.core.auth.services.users import change_user_status
from app.core.auth.services.users import create_user
from app.core.auth.services.users import get_roles_by_ids
from app.core.auth.services.users import get_user
from app.core.auth.services.users import list_users
from app.core.auth.services.users import resolve_assignable_roles
from app.core.auth.services.users import soft_delete_user
from app.core.auth.services.users import update_user
from app.core.config import get_settings
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import NotFoundError
from app.core.restore import restore_cutoff

# These suites exercise the live set; the window is inert but the services now
# require it explicitly, as the routes derive it.
_CUTOFF = restore_cutoff(get_settings())


def _caller(permissions: set[str]) -> SessionUser:
    return SessionUser(
        id=uuid4(),
        email="caller@example.com",
        email_verified=True,
        first_name=None,
        last_name=None,
        provider="local",
        permissions=frozenset(permissions),
    )


_NO_FILTERS = UserFilters()


@pytest_asyncio.fixture
async def admin_role(db_session: AsyncSession) -> Role:
    role = Role(name="admin", description="Admin role", permissions=["users:read"])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def participant_role(db_session: AsyncSession) -> Role:
    role = Role(name="participant", description="Participant role", permissions=["users:read"])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest.mark.integration
async def test_create_user_persists_row(db_session: AsyncSession, admin_role: Role) -> None:
    user = await create_user(
        db_session,
        email="ada@example.com",
        first_name="Ada",
        last_name="Lovelace",
        password=SecretStr("correct-horse-battery-staple"),
        roles=[admin_role],
    )

    assert user.id is not None
    assert user.email == "ada@example.com"
    assert user.first_name == "Ada"
    assert user.last_name == "Lovelace"
    assert user.status is UserStatus.INVITED
    assert user.email_verified_at is None
    assert user.password is not None
    assert user.password != "correct-horse-battery-staple"
    assert [r.id for r in user.roles] == [admin_role.id]


@pytest.mark.integration
async def test_create_user_with_email_verified_stamps_timestamp(db_session: AsyncSession, admin_role: Role) -> None:
    user = await create_user(
        db_session,
        email="verified@example.com",
        email_verified=True,
        roles=[admin_role],
    )

    assert user.email_verified_at is not None


@pytest.mark.integration
async def test_create_user_respects_status_override(db_session: AsyncSession, admin_role: Role) -> None:
    user = await create_user(
        db_session,
        email="active@example.com",
        status=UserStatus.ACTIVE,
        roles=[admin_role],
    )

    assert user.status is UserStatus.ACTIVE


@pytest.mark.integration
async def test_create_user_without_password_stores_none(db_session: AsyncSession, admin_role: Role) -> None:
    user = await create_user(db_session, email="passwordless@example.com", roles=[admin_role])

    assert user.password is None


@pytest.mark.integration
async def test_create_user_assigns_multiple_roles_deduped(
    db_session: AsyncSession, admin_role: Role, participant_role: Role
) -> None:
    user = await create_user(
        db_session,
        email="multi@example.com",
        roles=[admin_role, participant_role, admin_role],
    )

    assert {r.id for r in user.roles} == {admin_role.id, participant_role.id}


@pytest.mark.integration
async def test_create_user_rejects_empty_role_list(db_session: AsyncSession) -> None:
    with pytest.raises(BadRequestError, match="at least one role"):
        await create_user(db_session, email="no-roles@example.com", roles=[])


@pytest.mark.integration
async def test_get_roles_by_ids_rejects_unknown_role_id(db_session: AsyncSession) -> None:
    with pytest.raises(BadRequestError, match="Unknown role"):
        await get_roles_by_ids(db_session, [uuid4()])


@pytest.mark.integration
async def test_get_roles_by_ids_rejects_soft_deleted_role(db_session: AsyncSession, admin_role: Role) -> None:
    admin_role.deleted_at = datetime.now(UTC)
    db_session.add(admin_role)
    await db_session.flush()

    with pytest.raises(BadRequestError, match="Unknown role"):
        await get_roles_by_ids(db_session, [admin_role.id])


@pytest.mark.integration
async def test_get_roles_by_ids_rejects_inactive_role(db_session: AsyncSession, admin_role: Role) -> None:
    admin_role.is_active = False
    db_session.add(admin_role)
    await db_session.flush()

    with pytest.raises(BadRequestError, match="inactive role"):
        await get_roles_by_ids(db_session, [admin_role.id])


@pytest.mark.integration
async def test_create_user_rejects_duplicate_email(db_session: AsyncSession, admin_role: Role) -> None:
    await create_user(db_session, email="dup@example.com", roles=[admin_role])

    with pytest.raises(ConflictError):
        await create_user(db_session, email="dup@example.com", roles=[admin_role])


@pytest.mark.integration
async def test_create_user_allows_reusing_email_after_soft_delete(db_session: AsyncSession, admin_role: Role) -> None:
    first = await create_user(db_session, email="reuse@example.com", roles=[admin_role])
    await soft_delete_user(db_session, first, by_id=uuid4())

    second = await create_user(db_session, email="reuse@example.com", roles=[admin_role])

    assert second.id != first.id
    assert second.email == "reuse@example.com"


@pytest.mark.integration
async def test_get_user_returns_existing(db_session: AsyncSession, admin_role: Role) -> None:
    created = await create_user(db_session, email="get-me@example.com", roles=[admin_role])

    fetched = await get_user(db_session, created.id)

    assert fetched.id == created.id
    assert [r.id for r in fetched.roles] == [admin_role.id]


@pytest.mark.integration
async def test_get_user_raises_for_unknown_id(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await get_user(db_session, uuid4())


@pytest.mark.integration
async def test_get_user_excludes_soft_deleted(db_session: AsyncSession, admin_role: Role) -> None:
    user = await create_user(db_session, email="ghost@example.com", roles=[admin_role])
    await soft_delete_user(db_session, user, by_id=uuid4())

    with pytest.raises(NotFoundError):
        await get_user(db_session, user.id)


@pytest.mark.integration
async def test_get_user_for_update_overwrites_a_stale_cached_row(db_session: AsyncSession, admin_role: Role) -> None:
    """`for_update` must hand back the locked DB state, not the values an earlier read cached.

    The out-of-band write uses ``synchronize_session=False`` so the in-memory instance keeps
    the pre-update value — exactly the state a concurrent writer would leave behind, and what
    `populate_existing` on the locked read has to discard. Without it every post-lock re-check
    in the registration flow reads pre-lock status and the row lock protects nothing.
    """
    user = await create_user(db_session, email="stale@example.com", status=UserStatus.PENDING, roles=[admin_role])
    await db_session.execute(
        update(User)
        .where(col(User.id) == user.id)
        .values(status=UserStatus.ACTIVE)
        .execution_options(synchronize_session=False)
    )
    assert user.status is UserStatus.PENDING

    locked = await get_user(db_session, user.id, for_update=True)

    assert locked is user
    assert locked.status is UserStatus.ACTIVE


@pytest.mark.integration
async def test_get_user_excludes_soft_deleted_role_from_roles(
    db_session: AsyncSession, admin_role: Role, participant_role: Role
) -> None:
    user = await create_user(
        db_session,
        email="ghost-role-after@example.com",
        roles=[admin_role, participant_role],
    )
    user_id = user.id
    admin_role.deleted_at = datetime.now(UTC)
    db_session.add(admin_role)
    await db_session.flush()
    db_session.expunge_all()

    fetched = await get_user(db_session, user_id)

    assert [r.id for r in fetched.roles] == [participant_role.id]


@pytest.mark.integration
async def test_list_users_returns_page_and_total(db_session: AsyncSession, admin_role: Role) -> None:
    for i in range(3):
        await create_user(db_session, email=f"user{i}@example.com", roles=[admin_role])

    items, total = await list_users(
        db_session, deleted_cutoff=_CUTOFF, filters=_NO_FILTERS, order_by="created_at", limit=2, offset=0
    )

    assert total == 3
    assert len(items) == 2
    assert all(r.id == admin_role.id for u in items for r in u.roles)


@pytest.mark.integration
async def test_list_users_excludes_soft_deleted_role_from_roles(
    db_session: AsyncSession, admin_role: Role, participant_role: Role
) -> None:
    await create_user(
        db_session,
        email="list-ghost-role@example.com",
        roles=[admin_role, participant_role],
    )
    admin_role.deleted_at = datetime.now(UTC)
    db_session.add(admin_role)
    await db_session.flush()
    db_session.expunge_all()

    items, _ = await list_users(
        db_session, deleted_cutoff=_CUTOFF, filters=_NO_FILTERS, order_by="created_at", limit=10, offset=0
    )

    [only] = items
    assert [r.id for r in only.roles] == [participant_role.id]


@pytest.mark.integration
async def test_list_users_excludes_soft_deleted(db_session: AsyncSession, admin_role: Role) -> None:
    keep = await create_user(db_session, email="keep@example.com", roles=[admin_role])
    drop = await create_user(db_session, email="drop@example.com", roles=[admin_role])
    await soft_delete_user(db_session, drop, by_id=uuid4())

    items, total = await list_users(
        db_session, deleted_cutoff=_CUTOFF, filters=_NO_FILTERS, order_by="created_at", limit=10, offset=0
    )

    assert total == 1
    assert [u.id for u in items] == [keep.id]


@pytest.mark.integration
async def test_list_users_filters_by_status(db_session: AsyncSession, admin_role: Role) -> None:
    await create_user(db_session, email="active@example.com", status=UserStatus.ACTIVE, roles=[admin_role])
    await create_user(db_session, email="invited@example.com", status=UserStatus.INVITED, roles=[admin_role])

    items, total = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=UserFilters(status=UserStatus.ACTIVE),
        order_by="created_at",
        limit=10,
        offset=0,
    )

    assert total == 1
    assert [u.email for u in items] == ["active@example.com"]


@pytest.mark.integration
async def test_list_users_filters_by_email_substring_case_insensitive(
    db_session: AsyncSession, admin_role: Role
) -> None:
    await create_user(db_session, email="hit@example.com", roles=[admin_role])
    await create_user(db_session, email="ada-hit@other.com", roles=[admin_role])
    await create_user(db_session, email="miss@example.com", roles=[admin_role])

    items, total = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=UserFilters(email="HIT"),  # mixed case, substring
        order_by="created_at",
        limit=10,
        offset=0,
    )

    assert total == 2
    assert {u.email for u in items} == {"hit@example.com", "ada-hit@other.com"}


@pytest.mark.integration
async def test_list_users_filters_by_first_name_substring_case_insensitive(
    db_session: AsyncSession, admin_role: Role
) -> None:
    await create_user(db_session, email="ada@example.com", first_name="Ada", roles=[admin_role])
    await create_user(db_session, email="grace@example.com", first_name="Grace", roles=[admin_role])
    await create_user(db_session, email="anon@example.com", roles=[admin_role])  # null first_name

    items, total = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=UserFilters(first_name="aD"),  # mixed case, substring
        order_by="created_at",
        limit=10,
        offset=0,
    )

    assert total == 1
    assert [u.email for u in items] == ["ada@example.com"]


@pytest.mark.integration
async def test_list_users_filters_by_last_name_substring_case_insensitive(
    db_session: AsyncSession, admin_role: Role
) -> None:
    await create_user(db_session, email="ada@example.com", last_name="Lovelace", roles=[admin_role])
    await create_user(db_session, email="grace@example.com", last_name="Hopper", roles=[admin_role])

    items, total = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=UserFilters(last_name="LOVE"),
        order_by="created_at",
        limit=10,
        offset=0,
    )

    assert total == 1
    assert [u.email for u in items] == ["ada@example.com"]


@pytest.mark.integration
async def test_list_users_filters_by_role(db_session: AsyncSession, admin_role: Role, participant_role: Role) -> None:
    await create_user(db_session, email="admin-only@example.com", roles=[admin_role])
    await create_user(db_session, email="participant-only@example.com", roles=[participant_role])

    items, total = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=UserFilters(role_id=admin_role.id),
        order_by="created_at",
        limit=10,
        offset=0,
    )

    assert total == 1
    assert [u.email for u in items] == ["admin-only@example.com"]


@pytest.mark.integration
async def test_list_users_role_filter_does_not_duplicate_multi_role_users(
    db_session: AsyncSession, admin_role: Role, participant_role: Role
) -> None:
    # Regression: a JOIN-based filter on the m2m link table would emit one row
    # per matching role for each user. The IN-subquery form must return each
    # matching user exactly once even when they hold the filtered role
    # alongside others.
    await create_user(db_session, email="multi@example.com", roles=[admin_role, participant_role])

    items, total = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=UserFilters(role_id=admin_role.id),
        order_by="created_at",
        limit=10,
        offset=0,
    )

    assert total == 1
    assert [u.email for u in items] == ["multi@example.com"]


@pytest.mark.integration
async def test_list_users_filters_compose_with_and(db_session: AsyncSession, admin_role: Role) -> None:
    await create_user(
        db_session,
        email="ada-active@example.com",
        first_name="Ada",
        status=UserStatus.ACTIVE,
        roles=[admin_role],
    )
    # Same first name, wrong status — must be excluded by the AND.
    await create_user(
        db_session,
        email="ada-invited@example.com",
        first_name="Ada",
        status=UserStatus.INVITED,
        roles=[admin_role],
    )
    # Same status, wrong first name — must be excluded by the AND.
    await create_user(
        db_session,
        email="grace-active@example.com",
        first_name="Grace",
        status=UserStatus.ACTIVE,
        roles=[admin_role],
    )

    items, total = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=UserFilters(status=UserStatus.ACTIVE, first_name="ada"),
        order_by="created_at",
        limit=10,
        offset=0,
    )

    assert total == 1
    assert [u.email for u in items] == ["ada-active@example.com"]


@pytest.mark.integration
async def test_list_users_returns_empty_when_filters_match_nothing(db_session: AsyncSession, admin_role: Role) -> None:
    await create_user(db_session, email="present@example.com", roles=[admin_role])

    items, total = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=UserFilters(email="no-such-substring"),
        order_by="created_at",
        limit=10,
        offset=0,
    )

    assert total == 0
    assert items == []


@pytest.mark.integration
async def test_list_users_default_order_is_created_at_ascending(db_session: AsyncSession, admin_role: Role) -> None:
    # `now()` is constant within a transaction, so we stamp distinct
    # `created_at` values explicitly to make the primary sort decisive
    # (otherwise the result order collapses onto the `id` tie-breaker).
    first = await create_user(db_session, email="first@example.com", roles=[admin_role])
    second = await create_user(db_session, email="second@example.com", roles=[admin_role])
    third = await create_user(db_session, email="third@example.com", roles=[admin_role])
    base = datetime.now(UTC)
    for offset_seconds, user in enumerate((first, second, third)):
        user.created_at = base + timedelta(seconds=offset_seconds)
    await db_session.flush()

    items, _ = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=_NO_FILTERS,
        order_by="created_at",
        limit=10,
        offset=0,
    )

    assert [u.id for u in items] == [first.id, second.id, third.id]


@pytest.mark.integration
async def test_list_users_order_by_updated_at(db_session: AsyncSession, admin_role: Role) -> None:
    first = await create_user(db_session, email="first@example.com", roles=[admin_role])
    second = await create_user(db_session, email="second@example.com", roles=[admin_role])
    third = await create_user(db_session, email="third@example.com", roles=[admin_role])
    # Reverse-stamped: oldest `updated_at` on `third`, newest on `first`.
    # ASC must therefore return third → second → first.
    base = datetime.now(UTC)
    for offset_seconds, user in enumerate((third, second, first)):
        user.updated_at = base + timedelta(seconds=offset_seconds)
    await db_session.flush()

    items, _ = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=_NO_FILTERS,
        order_by="updated_at",
        limit=10,
        offset=0,
    )

    assert [u.id for u in items] == [third.id, second.id, first.id]


@pytest.mark.integration
async def test_list_users_order_by_updated_at_descending(db_session: AsyncSession, admin_role: Role) -> None:
    first = await create_user(db_session, email="first@example.com", roles=[admin_role])
    second = await create_user(db_session, email="second@example.com", roles=[admin_role])
    third = await create_user(db_session, email="third@example.com", roles=[admin_role])
    base = datetime.now(UTC)
    for offset_seconds, user in enumerate((first, second, third)):
        user.updated_at = base + timedelta(seconds=offset_seconds)
    await db_session.flush()

    items, _ = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=_NO_FILTERS,
        order_by="-updated_at",
        limit=10,
        offset=0,
    )

    assert [u.id for u in items] == [third.id, second.id, first.id]


@pytest.mark.integration
async def test_list_users_filter_excludes_soft_deleted(db_session: AsyncSession, admin_role: Role) -> None:
    # Regression: filters must compose onto `User.live_select()`, not replace
    # its `deleted_at IS NULL` predicate. A soft-deleted row whose email
    # matches the substring must still be excluded.
    keep = await create_user(db_session, email="alice-keep@example.com", roles=[admin_role])
    drop = await create_user(db_session, email="alice-drop@example.com", roles=[admin_role])
    await soft_delete_user(db_session, drop, by_id=uuid4())

    items, total = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=UserFilters(email="alice"),
        order_by="created_at",
        limit=10,
        offset=0,
    )

    assert total == 1
    assert [u.id for u in items] == [keep.id]


@pytest.mark.integration
async def test_list_users_email_filter_escapes_like_wildcards(db_session: AsyncSession, admin_role: Role) -> None:
    # Regression: a bare `%` from the client used to be passed straight into
    # `ilike('%' + value + '%')`, turning the filter into "match anything".
    # Post-fix it must be escaped and treated as a literal character.
    await create_user(db_session, email="alice@example.com", roles=[admin_role])
    await create_user(db_session, email="bob@example.com", roles=[admin_role])

    items, total = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=UserFilters(email="%"),
        order_by="created_at",
        limit=10,
        offset=0,
    )

    assert total == 0
    assert items == []


@pytest.mark.integration
async def test_list_users_order_by_descending_email(db_session: AsyncSession, admin_role: Role) -> None:
    await create_user(db_session, email="a@example.com", roles=[admin_role])
    await create_user(db_session, email="c@example.com", roles=[admin_role])
    await create_user(db_session, email="b@example.com", roles=[admin_role])

    items, _ = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=_NO_FILTERS,
        order_by="-email",
        limit=10,
        offset=0,
    )

    assert [u.email for u in items] == ["c@example.com", "b@example.com", "a@example.com"]


@pytest.mark.integration
async def test_list_users_order_by_status(db_session: AsyncSession, admin_role: Role) -> None:
    await create_user(db_session, email="invited@example.com", status=UserStatus.INVITED, roles=[admin_role])
    await create_user(db_session, email="active@example.com", status=UserStatus.ACTIVE, roles=[admin_role])
    await create_user(db_session, email="pending@example.com", status=UserStatus.PENDING, roles=[admin_role])

    items, _ = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=_NO_FILTERS,
        order_by="status",
        limit=10,
        offset=0,
    )

    # `status` is a Postgres ENUM, so ORDER BY follows the enum's declaration
    # order, not the lexical order of the stored values (ACTIVE, PENDING,
    # INVITED, INACTIVE — see `UserStatus`).
    assert [u.status for u in items] == [UserStatus.ACTIVE, UserStatus.PENDING, UserStatus.INVITED]


@pytest.mark.integration
async def test_list_users_order_by_first_name(db_session: AsyncSession, admin_role: Role) -> None:
    await create_user(db_session, email="b@example.com", first_name="Bea", roles=[admin_role])
    await create_user(db_session, email="a@example.com", first_name="Ada", roles=[admin_role])
    await create_user(db_session, email="c@example.com", first_name="Cal", roles=[admin_role])

    items, _ = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=_NO_FILTERS,
        order_by="first_name",
        limit=10,
        offset=0,
    )

    assert [u.first_name for u in items] == ["Ada", "Bea", "Cal"]


@pytest.mark.integration
async def test_list_users_order_by_last_name_descending(db_session: AsyncSession, admin_role: Role) -> None:
    await create_user(db_session, email="a@example.com", last_name="Adams", roles=[admin_role])
    await create_user(db_session, email="z@example.com", last_name="Zhang", roles=[admin_role])
    await create_user(db_session, email="m@example.com", last_name="Mori", roles=[admin_role])

    items, _ = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=_NO_FILTERS,
        order_by="-last_name",
        limit=10,
        offset=0,
    )

    assert [u.last_name for u in items] == ["Zhang", "Mori", "Adams"]


@pytest.mark.integration
async def test_list_users_order_by_nullable_asc_places_nulls_last(db_session: AsyncSession, admin_role: Role) -> None:
    # Postgres' default for ASC is already NULLS LAST, so this also pins the
    # baseline that the DESC test below diverges from.
    await create_user(db_session, email="ada@example.com", first_name="Ada", roles=[admin_role])
    await create_user(db_session, email="bea@example.com", first_name="Bea", roles=[admin_role])
    await create_user(db_session, email="anon@example.com", roles=[admin_role])  # NULL first_name

    items, _ = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=_NO_FILTERS,
        order_by="first_name",
        limit=10,
        offset=0,
    )

    assert [u.first_name for u in items] == ["Ada", "Bea", None]


@pytest.mark.integration
async def test_list_users_order_by_nullable_desc_still_places_nulls_last(
    db_session: AsyncSession, admin_role: Role
) -> None:
    # Regression: Postgres defaults to NULLS FIRST for DESC, which would
    # surface NULL-named rows at the top of the first page. `apply_order_by`
    # forces NULLS LAST for symmetry — without it, the anon user lands at
    # index 0 instead of index 2.
    await create_user(db_session, email="ada@example.com", first_name="Ada", roles=[admin_role])
    await create_user(db_session, email="bea@example.com", first_name="Bea", roles=[admin_role])
    await create_user(db_session, email="anon@example.com", roles=[admin_role])  # NULL first_name

    items, _ = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=_NO_FILTERS,
        order_by="-first_name",
        limit=10,
        offset=0,
    )

    assert [u.first_name for u in items] == ["Bea", "Ada", None]


@pytest.mark.integration
async def test_list_users_order_by_nullable_ties_break_on_id(db_session: AsyncSession, admin_role: Role) -> None:
    # All three rows share `first_name=NULL`, so the primary sort is a no-op
    # tie and ordering must collapse onto the `id` secondary key. Without
    # the tie-breaker, paginated NULL-heavy result sets would shuffle
    # between pages.
    a = await create_user(db_session, email="a@example.com", roles=[admin_role])
    b = await create_user(db_session, email="b@example.com", roles=[admin_role])
    c = await create_user(db_session, email="c@example.com", roles=[admin_role])

    items, _ = await list_users(
        db_session,
        deleted_cutoff=_CUTOFF,
        filters=_NO_FILTERS,
        order_by="-first_name",
        limit=10,
        offset=0,
    )

    assert [u.id for u in items] == sorted([a.id, b.id, c.id])


@pytest.mark.integration
async def test_update_user_writes_only_supplied_fields(db_session: AsyncSession, admin_role: Role) -> None:
    user = await create_user(
        db_session,
        email="patch@example.com",
        first_name="Original",
        last_name="Surname",
        roles=[admin_role],
    )

    updated = await update_user(db_session, user, UserUpdateChanges(first_name="Renamed"))

    assert updated.first_name == "Renamed"
    assert updated.last_name == "Surname"
    assert updated.email == "patch@example.com"
    assert [r.id for r in updated.roles] == [admin_role.id]


@pytest.mark.integration
async def test_update_user_replaces_roles(db_session: AsyncSession, admin_role: Role, participant_role: Role) -> None:
    user = await create_user(db_session, email="roles@example.com", roles=[admin_role])

    updated = await update_user(db_session, user, UserUpdateChanges(roles=[participant_role]))

    assert [r.id for r in updated.roles] == [participant_role.id]


@pytest.mark.integration
async def test_update_user_retains_a_held_inactive_role(
    db_session: AsyncSession, admin_role: Role, participant_role: Role
) -> None:
    # The API projection hides an inactive role, so a full-set PATCH must not destroy it:
    # the caller never saw it and `get_roles_by_ids` would refuse it if they re-sent it.
    admin_role.is_active = False
    db_session.add(admin_role)
    await db_session.flush()
    user = await create_user(db_session, email="retain@example.com", roles=[admin_role])

    updated = await update_user(db_session, user, UserUpdateChanges(roles=[participant_role]))

    assert {r.id for r in updated.roles} == {participant_role.id, admin_role.id}


@pytest.mark.integration
async def test_update_user_role_replacement_removes_link_rows(
    db_session: AsyncSession, admin_role: Role, participant_role: Role
) -> None:
    user = await create_user(db_session, email="repl@example.com", roles=[admin_role, participant_role])

    await update_user(db_session, user, UserUpdateChanges(roles=[participant_role]))

    refreshed = await get_user(db_session, user.id)
    assert [r.id for r in refreshed.roles] == [participant_role.id]


def test_user_update_changes_rejects_unknown_keys() -> None:
    # Guards the service contract: fields outside the DTO (e.g. password,
    # email, status) must be rejected at construction time, never silently
    # forwarded to the DB.
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        UserUpdateChanges.model_validate({"password": "brand-new"})


@pytest.mark.integration
async def test_soft_delete_stamps_deleted_at(db_session: AsyncSession, admin_role: Role) -> None:
    user = await create_user(db_session, email="bye@example.com", roles=[admin_role])

    deleted = await soft_delete_user(db_session, user, by_id=uuid4())

    assert deleted.deleted_at is not None


@pytest.mark.integration
async def test_soft_delete_cascades_to_provider_identities(db_session: AsyncSession, admin_role: Role) -> None:
    """Without this, a live identity row keeps resolving to the tombstoned account and
    permanently blocks that IdP subject from ever reaching a recreated account for the
    same email (`app/core/auth/services/oidc.py::_get_identity` wins before the
    email-link path runs).
    """
    user = await create_user(db_session, email="bye@example.com", roles=[admin_role])
    db_session.add(ProviderIdentity(provider="google", subject="google-sub-123", user_id=user.id))
    await db_session.flush()

    await soft_delete_user(db_session, user, by_id=uuid4())

    result = await db_session.execute(ProviderIdentity.live_select().where(col(ProviderIdentity.user_id) == user.id))
    assert result.scalar_one_or_none() is None


@pytest.mark.integration
async def test_user_role_link_row_exists_after_create(db_session: AsyncSession, admin_role: Role) -> None:
    """Sanity check that the join row was actually written, not just the in-memory list."""
    user = await create_user(db_session, email="join@example.com", roles=[admin_role])

    result = await db_session.execute(select(User).where(col(User.id) == user.id))
    persisted = result.scalar_one()
    fetched = await get_user(db_session, persisted.id)
    assert [r.id for r in fetched.roles] == [admin_role.id]


@pytest.mark.unit
def test_assert_can_assign_roles_blocks_elevated_role_without_permission() -> None:
    caller = _caller({"users:update"})

    with pytest.raises(ForbiddenError, match="cannot assign role"):
        assert_can_assign_roles(caller, [Role(name="admin")], current_roles=[])


@pytest.mark.unit
def test_assert_can_assign_roles_allows_elevated_role_with_permission() -> None:
    caller = _caller({Permission.USERS_MANAGE_ADMIN.value})

    assert_can_assign_roles(caller, [Role(name="admin")], current_roles=[])  # no raise


@pytest.mark.unit
def test_assert_can_assign_roles_allows_non_elevated_roles() -> None:
    caller = _caller(set())

    assert_can_assign_roles(caller, [Role(name="viewer"), Role(name="annotator")], current_roles=[])  # no raise


@pytest.mark.unit
def test_assert_can_assign_roles_blocks_removing_elevated_role_without_permission() -> None:
    caller = _caller({Permission.USERS_UPDATE.value})

    with pytest.raises(ForbiddenError, match="cannot revoke role"):
        assert_can_assign_roles(caller, [Role(name="viewer")], current_roles=[Role(name="admin")])


@pytest.mark.unit
def test_assert_can_assign_roles_allows_removing_elevated_role_with_permission() -> None:
    caller = _caller({Permission.USERS_MANAGE_ADMIN.value})

    assert_can_assign_roles(caller, [Role(name="viewer")], current_roles=[Role(name="admin")])  # no raise


@pytest.mark.unit
def test_assert_can_assign_roles_allows_removing_non_elevated_role() -> None:
    caller = _caller({Permission.USERS_UPDATE.value})

    assert_can_assign_roles(caller, [Role(name="annotator")], current_roles=[Role(name="viewer")])  # no raise


@pytest.mark.unit
def test_assert_can_assign_roles_blocks_owner_without_manage_admin() -> None:
    # The invite-path escalation: `users:invite` is delegable to a custom role, so
    # granting `owner` must require the non-delegable elevation key like `admin` does.
    caller = _caller({Permission.USERS_INVITE.value})

    with pytest.raises(ForbiddenError, match="cannot assign role"):
        assert_can_assign_roles(caller, [Role(name="owner")], current_roles=[])


@pytest.mark.unit
def test_assert_can_delete_user_blocks_owner_target_without_manage_admin() -> None:
    caller = _caller({Permission.USERS_DELETE.value})
    target = User(email="owner@example.com")
    target.roles = [Role(name="owner")]

    with pytest.raises(ForbiddenError, match="cannot delete a user holding role"):
        assert_can_delete_user(caller, target)


@pytest.mark.unit
def test_assert_can_delete_user_blocks_admin_target_without_manage_admin() -> None:
    caller = _caller({Permission.USERS_DELETE.value})
    target = User(email="admin@example.com")
    target.roles = [Role(name="admin")]

    with pytest.raises(ForbiddenError, match="cannot delete a user holding role"):
        assert_can_delete_user(caller, target)


@pytest.mark.unit
def test_assert_can_delete_user_allows_admin_target_with_manage_admin() -> None:
    caller = _caller({Permission.USERS_DELETE.value, Permission.USERS_MANAGE_ADMIN.value})
    target = User(email="admin@example.com")
    target.roles = [Role(name="admin")]

    assert_can_delete_user(caller, target)  # no raise


@pytest.mark.unit
def test_assert_can_delete_user_allows_non_elevated_target() -> None:
    caller = _caller({Permission.USERS_DELETE.value})
    target = User(email="viewer@example.com")
    target.roles = [Role(name="viewer")]

    assert_can_delete_user(caller, target)  # no raise


@pytest.mark.integration
async def test_resolve_assignable_roles_returns_roles_when_authorized(
    db_session: AsyncSession, admin_role: Role, participant_role: Role
) -> None:
    caller = _caller({Permission.USERS_MANAGE_ADMIN.value})

    roles = await resolve_assignable_roles(db_session, caller, [participant_role.id, admin_role.id], current_roles=[])

    assert [role.id for role in roles] == [participant_role.id, admin_role.id]


@pytest.mark.integration
async def test_resolve_assignable_roles_blocks_elevated_role_without_permission(
    db_session: AsyncSession, admin_role: Role
) -> None:
    caller = _caller({"users:update"})

    with pytest.raises(ForbiddenError, match="cannot assign role"):
        await resolve_assignable_roles(db_session, caller, [admin_role.id], current_roles=[])


@pytest.mark.integration
async def test_resolve_assignable_roles_blocks_removing_elevated_role_without_permission(
    db_session: AsyncSession, admin_role: Role, participant_role: Role
) -> None:
    """Removal is gated too — a caller lacking `users:manage_admin` can't strip admin."""
    caller = _caller({Permission.USERS_UPDATE.value})

    with pytest.raises(ForbiddenError, match="cannot revoke role"):
        await resolve_assignable_roles(db_session, caller, [participant_role.id], current_roles=[admin_role])


@pytest.mark.integration
async def test_resolve_assignable_roles_rejects_unknown_role(db_session: AsyncSession) -> None:
    """Existence is validated before authorization — an unknown id surfaces as 400, not 403."""
    caller = _caller({Permission.USERS_MANAGE_ADMIN.value})

    with pytest.raises(BadRequestError, match="Unknown role"):
        await resolve_assignable_roles(db_session, caller, [uuid4()], current_roles=[])


@pytest.mark.integration
async def test_resolve_assignable_roles_rejects_held_inactive_role(
    db_session: AsyncSession, participant_role: Role
) -> None:
    # An inactive role is never assignable — even one the user already holds and echoes back on update.
    caller = _caller({Permission.USERS_UPDATE.value})
    participant_role.is_active = False
    db_session.add(participant_role)
    await db_session.flush()

    with pytest.raises(BadRequestError, match="inactive role"):
        await resolve_assignable_roles(db_session, caller, [participant_role.id], current_roles=[participant_role])


@pytest.mark.integration
async def test_user_response_hides_inactive_role(
    db_session: AsyncSession, admin_role: Role, participant_role: Role
) -> None:
    user = await create_user(db_session, email="inactive-role-embed@example.com", roles=[admin_role, participant_role])
    user_id = user.id
    admin_role.is_active = False
    db_session.add(admin_role)
    await db_session.flush()
    db_session.expunge_all()

    response = UserResponse.from_user(await get_user(db_session, user_id))

    assert [role.id for role in response.roles] == [participant_role.id]


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        (UserStatus.ACTIVE, UserStatus.INACTIVE),
        (UserStatus.INACTIVE, UserStatus.ACTIVE),
    ],
)
async def test_change_user_status_switches_between_active_and_inactive(
    db_session: AsyncSession, admin_role: Role, initial: UserStatus, target: SettableUserStatus
) -> None:
    user = await create_user(db_session, email="switch@example.com", status=initial, roles=[admin_role])

    updated = await change_user_status(db_session, user, target)

    assert updated.status is target
    await db_session.refresh(user)
    assert user.status is target


@pytest.mark.integration
async def test_change_user_status_is_idempotent_for_the_current_status(
    db_session: AsyncSession, admin_role: Role
) -> None:
    """Re-sending the status a user already holds succeeds so a bulk retry doesn't half-fail."""
    user = await create_user(db_session, email="same@example.com", status=UserStatus.ACTIVE, roles=[admin_role])

    updated = await change_user_status(db_session, user, UserStatus.ACTIVE)

    assert updated.status is UserStatus.ACTIVE


@pytest.mark.integration
@pytest.mark.parametrize("initial", [UserStatus.INVITED, UserStatus.PENDING])
async def test_change_user_status_rejects_onboarding_source_status(
    db_session: AsyncSession, admin_role: Role, initial: UserStatus
) -> None:
    user = await create_user(db_session, email="onboarding@example.com", status=initial, roles=[admin_role])

    with pytest.raises(ConflictError, match="can be activated or deactivated"):
        await change_user_status(db_session, user, UserStatus.ACTIVE)

    assert user.status is initial


@pytest.mark.unit
def test_assert_can_manage_status_blocks_admin_target_without_manage_admin() -> None:
    caller = _caller({Permission.USERS_UPDATE.value})
    target = User(email="admin@example.com")
    target.roles = [Role(name="admin")]

    with pytest.raises(ForbiddenError, match="cannot change the status of a user holding role"):
        assert_can_manage_status(caller, target)


@pytest.mark.unit
def test_assert_can_manage_status_allows_admin_target_with_manage_admin() -> None:
    caller = _caller({Permission.USERS_UPDATE.value, Permission.USERS_MANAGE_ADMIN.value})
    target = User(email="admin@example.com")
    target.roles = [Role(name="admin")]

    assert_can_manage_status(caller, target)  # no raise


@pytest.mark.unit
def test_assert_can_manage_status_allows_non_elevated_target() -> None:
    caller = _caller({Permission.USERS_UPDATE.value})
    target = User(email="viewer@example.com")
    target.roles = [Role(name="viewer")]

    assert_can_manage_status(caller, target)  # no raise


@pytest.mark.integration
async def test_update_user_ignores_an_omitted_consent_flag(db_session: AsyncSession, active_user: User) -> None:
    active_user.consent_emails = True
    await db_session.flush()

    updated = await update_user(db_session, active_user, UserUpdateChanges(first_name="Renamed"))

    assert updated.consent_emails is True
