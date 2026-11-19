"""API tests for `/api/v1/evaluation-groups/{group_id}/members`.

The end-to-end matrix for per-group roles: visibility (who can list), management
(who can add/replace/remove), and the two decisions that define the model — the
object-role *override* (a global manager demoted by a lesser in-group role) and
the *break-glass* admin that survives it.
"""

from collections.abc import Iterator
from datetime import date
from uuid import uuid4

import pytest
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.audit.enums import AuditAction
from app.core.audit.models import AuditLog
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.models import ObjectRoleAssignment
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import grant_roles
from app.core.auth.object_roles.service import remove_member
from app.core.auth.roles import Permission
from app.core.auth.schemas import MAX_INVITE_ROWS
from app.core.auth.services.users import create_user
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import EvaluationGroup
from tests.api.v1.conftest import as_user
from tests.api.v1.conftest import make_role

pytestmark = pytest.mark.integration

_GROUP = ObjectType.EVALUATION_GROUP
# Listing members is gated like `GET /evaluation-groups/{id}`: the caller needs
# the global `evaluation_groups:read` permission on top of group visibility.
_READ = [Permission.EVALUATION_GROUPS_READ.value]


async def _user(db: AsyncSession, permissions: list[str] | None = None) -> User:
    role = await make_role(db, permissions)
    return await create_user(db, email=f"{uuid4().hex[:8]}@example.com", roles=[role])


async def _group(
    db: AsyncSession,
    *,
    owner: User,
    access_level: EvaluationGroupAccessLevel = EvaluationGroupAccessLevel.INVITATION_ONLY,
) -> EvaluationGroup:
    group = EvaluationGroup(
        title="Engagement",
        description="A red-teaming engagement.",
        created_by_id=owner.id,
        access_level=access_level,
        status=PublicationStatus.PUBLISHED,
        start_date=date(2026, 3, 1),
    )
    db.add(group)
    await db.flush()
    await db.refresh(group)
    return group


async def _assign(db: AsyncSession, group: EvaluationGroup, user: User, role: Role) -> None:
    await grant_roles(db, _GROUP, group.id, user.id, [role])


async def _owned_group(db: AsyncSession, system_roles: dict[str, Role], owner: User) -> EvaluationGroup:
    """An invitation-only group whose creator holds the in-group `owner` role."""
    group = await _group(db, owner=owner)
    await _assign(db, group, owner, system_roles["owner"])
    return group


def _members_url(group: EvaluationGroup) -> str:
    return f"/api/v1/evaluation-groups/{group.id}/members"


# --- visibility (GET list) --------------------------------------------------


async def test_owner_can_list_members(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session, permissions=_READ)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.get(_members_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 1  # the owner themselves
    assert body["items"][0]["user"]["status"] == owner.status.value


async def test_member_can_list_members(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    member = await _user(db_session, permissions=_READ)
    await _assign(db_session, group, member, system_roles["red_teamer"])

    with as_user(member):
        response = await async_client_with_db.get(_members_url(group))

    assert response.status_code == status.HTTP_200_OK


async def test_non_member_gets_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    outsider = await _user(db_session, permissions=_READ)

    with as_user(outsider):
        response = await async_client_with_db.get(_members_url(group))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_list_members_search_and_role_id_params_thread_through(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Wiring representative: the route parses `?search=`/`?role_id=` and threads them into
    # list_members (the filtering branches are covered in the service test).
    owner = await _user(db_session, permissions=_READ)
    group = await _owned_group(db_session, system_roles, owner)
    red_teamer = await create_user(
        db_session, email="wiring-redteamer@example.com", roles=[await make_role(db_session, [])]
    )
    viewer = await create_user(db_session, email="wiring-viewer@example.com", roles=[await make_role(db_session, [])])
    await _assign(db_session, group, red_teamer, system_roles["red_teamer"])
    await _assign(db_session, group, viewer, system_roles["viewer"])

    with as_user(owner):
        by_search = await async_client_with_db.get(f"{_members_url(group)}?search=redteamer")
        by_role = await async_client_with_db.get(f"{_members_url(group)}?role_id={system_roles['red_teamer'].id}")

    assert by_search.status_code == status.HTTP_200_OK
    assert {m["user"]["email"] for m in by_search.json()["items"]} == {"wiring-redteamer@example.com"}
    assert {m["user"]["email"] for m in by_role.json()["items"]} == {"wiring-redteamer@example.com"}


async def test_list_members_requires_read_permission(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Read gate mirrors `GET /evaluation-groups/{id}`: the in-group `owner` is a
    # visible member, but without the global `evaluation_groups:read` permission
    # the listing is forbidden — the permission check precedes visibility.
    # NOT subsumed by the endpoint-guard sweep: `require_group_read` reads its
    # permission as a module global, invisible to the closure introspection.
    owner = await _user(db_session)  # no global permissions
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.get(_members_url(group))

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_public_group_visible_to_any_authenticated_user(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _group(db_session, owner=owner, access_level=EvaluationGroupAccessLevel.PUBLIC)
    await _assign(db_session, group, owner, system_roles["owner"])
    stranger = await _user(db_session, permissions=_READ)

    with as_user(stranger):
        response = await async_client_with_db.get(_members_url(group))

    assert response.status_code == status.HTTP_200_OK


# --- management (POST / PATCH / DELETE) -------------------------------------


async def test_owner_adds_member(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    target = await _user(db_session)

    with as_user(owner):
        response = await async_client_with_db.post(
            _members_url(group),
            json={"user_id": str(target.id), "role_ids": [str(system_roles["red_teamer"].id)]},
        )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert {r["name"] for r in body["roles"]} == {"red_teamer"}
    assert body["user"] == {
        "id": str(target.id),
        "email": target.email,
        "first_name": target.first_name,
        "last_name": target.last_name,
        "status": target.status.value,
    }


async def test_add_member_rejects_platform_admin_role(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    target = await _user(db_session)

    with as_user(owner):
        response = await async_client_with_db.post(
            _members_url(group),
            json={"user_id": str(target.id), "role_ids": [str(system_roles["admin"].id)]},
        )

    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_add_member_unknown_user_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.post(
            _members_url(group),
            json={"user_id": str(uuid4()), "role_ids": [str(system_roles["red_teamer"].id)]},
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_add_duplicate_member_409(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    target = await _user(db_session)
    await _assign(db_session, group, target, system_roles["viewer"])

    with as_user(owner):
        response = await async_client_with_db.post(
            _members_url(group),
            json={"user_id": str(target.id), "role_ids": [str(system_roles["red_teamer"].id)]},
        )

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_replace_member_roles(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    member = await _user(db_session)
    await _assign(db_session, group, member, system_roles["red_teamer"])

    with as_user(owner):
        response = await async_client_with_db.patch(
            f"{_members_url(group)}/{member.id}",
            json={"role_ids": [str(system_roles["viewer"].id), str(system_roles["annotator"].id)]},
        )

    assert response.status_code == status.HTTP_200_OK
    assert {r["name"] for r in response.json()["roles"]} == {"viewer", "annotator"}


async def test_remove_member(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    member = await _user(db_session)
    await _assign(db_session, group, member, system_roles["red_teamer"])

    with as_user(owner):
        response = await async_client_with_db.delete(f"{_members_url(group)}/{member.id}")

    assert response.status_code == status.HTTP_204_NO_CONTENT


async def test_remove_last_owner_conflicts(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The sole owner can't be removed — the group would be left ownerless.
    owner = await _user(db_session, permissions=_READ)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.delete(f"{_members_url(group)}/{owner.id}")

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_demote_last_owner_conflicts(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session, permissions=_READ)
    group = await _owned_group(db_session, system_roles, owner)

    with as_user(owner):
        response = await async_client_with_db.patch(
            f"{_members_url(group)}/{owner.id}",
            json={"role_ids": [str(system_roles["red_teamer"].id)]},
        )

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_demote_owner_allowed_with_co_owner(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session, permissions=_READ)
    group = await _owned_group(db_session, system_roles, owner)
    co_owner = await _user(db_session)
    await _assign(db_session, group, co_owner, system_roles["owner"])

    with as_user(owner):
        response = await async_client_with_db.patch(
            f"{_members_url(group)}/{co_owner.id}",
            json={"role_ids": [str(system_roles["red_teamer"].id)]},
        )

    assert response.status_code == status.HTTP_200_OK
    assert {r["name"] for r in response.json()["roles"]} == {"red_teamer"}


async def test_break_glass_admin_cannot_remove_last_owner(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The invariant is absolute: even a break-glass admin must hand the `owner`
    # role off rather than orphan the group.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    admin = await _user(db_session, permissions=[Permission.EVALUATION_GROUPS_MANAGE.value])

    with as_user(admin):
        response = await async_client_with_db.delete(f"{_members_url(group)}/{owner.id}")

    assert response.status_code == status.HTTP_409_CONFLICT


async def test_member_with_lesser_role_cannot_manage(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    member = await _user(db_session)
    await _assign(db_session, group, member, system_roles["red_teamer"])
    target = await _user(db_session)

    with as_user(member):
        response = await async_client_with_db.post(
            _members_url(group),
            json={"user_id": str(target.id), "role_ids": [str(system_roles["viewer"].id)]},
        )

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_global_manager_is_demoted_by_lesser_object_role(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The override: a user who carries manage_members GLOBALLY but holds only
    # red_teamer on this group cannot manage its members.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    global_manager = await _user(db_session, permissions=[Permission.EVALUATION_GROUPS_MANAGE_MEMBERS.value])
    await _assign(db_session, group, global_manager, system_roles["red_teamer"])
    target = await _user(db_session)

    with as_user(global_manager):
        response = await async_client_with_db.post(
            _members_url(group),
            json={"user_id": str(target.id), "role_ids": [str(system_roles["viewer"].id)]},
        )

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_break_glass_admin_manages_despite_lesser_object_role(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The kept super-permission: an admin assigned only red_teamer still manages.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    admin = await _user(db_session, permissions=[Permission.EVALUATION_GROUPS_MANAGE.value])
    await _assign(db_session, group, admin, system_roles["red_teamer"])
    target = await _user(db_session)

    with as_user(admin):
        response = await async_client_with_db.post(
            _members_url(group),
            json={"user_id": str(target.id), "role_ids": [str(system_roles["viewer"].id)]},
        )

    assert response.status_code == status.HTTP_201_CREATED


async def test_global_manager_without_membership_cannot_manage_public_group(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Managing requires object-scope permission — a global manage_members holder
    # who holds no role on the group is forbidden, even though the group is
    # public (global permissions grant no object authority).
    owner = await _user(db_session)
    group = await _group(db_session, owner=owner, access_level=EvaluationGroupAccessLevel.PUBLIC)
    manager = await _user(db_session, permissions=[Permission.EVALUATION_GROUPS_MANAGE_MEMBERS.value])
    target = await _user(db_session)

    with as_user(manager):
        response = await async_client_with_db.post(
            _members_url(group),
            json={"user_id": str(target.id), "role_ids": [str(system_roles["viewer"].id)]},
        )

    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_global_manager_cannot_see_private_group_without_membership(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Visibility bounds the gate: a private group is hidden (404) from a
    # non-member without break-glass, even one holding manage_members (and read)
    # globally — read clears the permission gate, visibility still yields 404.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    manager = await _user(db_session, permissions=[Permission.EVALUATION_GROUPS_MANAGE_MEMBERS.value, *_READ])

    with as_user(manager):
        response = await async_client_with_db.get(_members_url(group))

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_break_glass_admin_manages_private_group_without_membership(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    admin = await _user(db_session, permissions=[Permission.EVALUATION_GROUPS_MANAGE.value])
    target = await _user(db_session)

    with as_user(admin):
        response = await async_client_with_db.post(
            _members_url(group),
            json={"user_id": str(target.id), "role_ids": [str(system_roles["viewer"].id)]},
        )

    assert response.status_code == status.HTTP_201_CREATED


async def test_creator_without_owner_role_loses_group_access(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The headline rule: `created_by_id` is attribution, not authority. With the
    # creator's `owner` assignment removed, their own private group reads as
    # missing (404) — pins that no creator convenience fallback exists. A second
    # owner is assigned first so removing the creator doesn't trip the
    # last-owner guard (that invariant is exercised separately below).
    creator = await _user(db_session, permissions=_READ)
    group = await _owned_group(db_session, system_roles, creator)
    co_owner = await _user(db_session)
    await _assign(db_session, group, co_owner, system_roles["owner"])
    await remove_member(db_session, _GROUP, group.id, creator.id, by_id=uuid4())

    with as_user(creator):
        response = await async_client_with_db.get(f"/api/v1/evaluation-groups/{group.id}")

    assert response.status_code == status.HTTP_404_NOT_FOUND


def _bulk_url(group: EvaluationGroup) -> str:
    return f"{_members_url(group)}/bulk"


def _row(key: str, user_id: object, *roles: Role) -> dict[str, object]:
    return {"row_key": key, "data": {"user_id": str(user_id), "role_ids": [str(r.id) for r in roles]}}


async def test_bulk_add_adds_every_row_with_its_own_roles(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session, _READ)
    group = await _owned_group(db_session, system_roles, owner)
    first, second = await _user(db_session), await _user(db_session)
    first_id, second_id = first.id, second.id

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={
                "rows": [
                    _row("a", first_id, system_roles["red_teamer"]),
                    _row("b", second_id, system_roles["red_teamer"], system_roles["annotator"]),
                ]
            },
        )
        listed = await async_client_with_db.get(_members_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (body["succeeded"], body["failed"]) == (2, 0)
    roles_by_user = {m["user"]["id"]: {r["name"] for r in m["roles"]} for m in listed.json()["items"]}
    assert roles_by_user[str(first_id)] == {"red_teamer"}
    assert roles_by_user[str(second_id)] == {"red_teamer", "annotator"}
    audits = (
        (await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.MEMBER_ADD.value)))
        .scalars()
        .all()
    )
    assert {a.object_id for a in audits} == {first_id, second_id}


async def test_bulk_add_reports_failed_rows_and_keeps_the_rest(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # A partial failure must not silently drop the rest of an organization.
    owner = await _user(db_session, _READ)
    group = await _owned_group(db_session, system_roles, owner)
    existing, refused, fresh = await _user(db_session), await _user(db_session), await _user(db_session)
    await _assign(db_session, group, existing, system_roles["viewer"])
    existing_id, refused_id, fresh_id = existing.id, refused.id, fresh.id

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={
                "rows": [
                    _row("member", existing_id, system_roles["red_teamer"]),
                    _row("admin-role", refused_id, system_roles["admin"]),
                    _row("ghost", uuid4(), system_roles["red_teamer"]),
                    _row("fresh", fresh_id, system_roles["red_teamer"]),
                ]
            },
        )
        listed = await async_client_with_db.get(_members_url(group))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (body["succeeded"], body["failed"]) == (1, 3)
    statuses = {r["row_key"]: (r["status"], r["error"] and r["error"]["status"]) for r in body["results"]}
    assert statuses == {
        "member": ("failed", status.HTTP_409_CONFLICT),
        "admin-role": ("failed", status.HTTP_400_BAD_REQUEST),
        "ghost": ("failed", status.HTTP_404_NOT_FOUND),
        "fresh": ("ok", None),
    }
    roles_by_user = {m["user"]["id"]: {r["name"] for r in m["roles"]} for m in listed.json()["items"]}
    assert roles_by_user[str(existing_id)] == {"viewer"}
    assert roles_by_user[str(fresh_id)] == {"red_teamer"}


@pytest.fixture
def statements(db_session: AsyncSession) -> Iterator[list[str]]:
    seen: list[str] = []
    engine = db_session.bind.engine.sync_engine

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


async def test_bulk_add_looks_users_and_roles_up_once_for_the_envelope(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    statements: list[str],
) -> None:
    # "Add all members" sends a whole organization, so the lookups must not grow with the rows.
    owner = await _user(db_session, _READ)

    async def lookups(rows: int) -> tuple[int, int]:
        group = await _owned_group(db_session, system_roles, owner)
        user_ids = [(await _user(db_session)).id for _ in range(rows)]
        payload = {"rows": [_row(str(i), user_id, system_roles["red_teamer"]) for i, user_id in enumerate(user_ids)]}
        statements.clear()
        with as_user(owner):
            response = await async_client_with_db.post(_bulk_url(group), json=payload)
        assert response.json()["succeeded"] == rows
        selects = [sql for sql in statements if sql.lstrip().startswith("SELECT")]
        return sum("FROM users" in sql for sql in selects), sum("FROM roles" in sql for sql in selects)

    few, many = await lookups(2), await lookups(6)

    # Both counters must hit, or a changed FROM clause would make the comparison pass at (0, 0).
    assert all(few)
    assert many == few


async def test_bulk_add_dry_run_writes_nothing(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Dry-run rolls the whole session back, fixtures included, so only emptiness is checkable.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    target = await _user(db_session)

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={"rows": [_row("a", target.id, system_roles["red_teamer"])], "dry_run": True},
        )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert (body["dry_run"], body["succeeded"]) == (True, 1)
    assert (await db_session.execute(select(ObjectRoleAssignment))).scalars().all() == []
    assert (await db_session.execute(select(AuditLog))).scalars().all() == []


async def test_bulk_add_dry_run_records_no_audit(
    async_client_with_db: AsyncClient,
    db_session: AsyncSession,
    system_roles: dict[str, Role],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The rollback would hide an audit row anyway; the bulk contract wants it never written.
    calls: list[object] = []

    async def spy(*args: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("app.api.v1.evaluation_group_members.record_audit", spy)
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    target = await _user(db_session)

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={"rows": [_row("a", target.id, system_roles["red_teamer"])], "dry_run": True},
        )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["succeeded"] == 1
    assert calls == []


async def test_bulk_add_rejects_a_user_repeated_across_rows(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    target = await _user(db_session)

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={
                "rows": [
                    _row("a", target.id, system_roles["red_teamer"]),
                    _row("b", target.id, system_roles["annotator"]),
                ]
            },
        )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "Duplicate user_id" in response.text


async def test_single_add_puts_a_removed_deactivated_member_back(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # The console's Undo after a removal is this call; the account may have been deactivated meanwhile.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    member = await _user(db_session)
    body = {"user_id": str(member.id), "role_ids": [str(system_roles["viewer"].id)]}

    with as_user(owner):
        added = await async_client_with_db.post(_members_url(group), json=body)
        member.status = UserStatus.INACTIVE
        await db_session.flush()
        removed = await async_client_with_db.delete(f"{_members_url(group)}/{member.id}")
        restored = await async_client_with_db.post(_members_url(group), json=body)

    assert added.status_code == status.HTTP_201_CREATED
    assert removed.status_code == status.HTTP_204_NO_CONTENT
    assert restored.status_code == status.HTTP_201_CREATED
    assert [role["name"] for role in restored.json()["roles"]] == ["viewer"]


async def test_single_add_refuses_a_deactivated_user_who_was_never_a_member(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # Otherwise roles would pile up on an inactive account and take effect on its reactivation.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    deactivated = await create_user(
        db_session,
        email=f"{uuid4().hex[:8]}@example.com",
        roles=[await make_role(db_session)],
        status=UserStatus.INACTIVE,
    )

    with as_user(owner):
        response = await async_client_with_db.post(
            _members_url(group),
            json={"user_id": str(deactivated.id), "role_ids": [str(system_roles["viewer"].id)]},
        )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["type"] == "urn:redteam:error:user-deactivated"


async def test_bulk_add_fails_a_deactivated_row_and_adds_an_invited_one(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # An account still onboarding is added; its roles wait for activation, as with an invitation.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    deactivated = await create_user(
        db_session,
        email=f"{uuid4().hex[:8]}@example.com",
        roles=[await make_role(db_session)],
        status=UserStatus.INACTIVE,
    )
    invited = await _user(db_session)

    with as_user(owner):
        response = await async_client_with_db.post(
            _bulk_url(group),
            json={
                "rows": [
                    _row("deactivated", deactivated.id, system_roles["viewer"]),
                    _row("invited", invited.id, system_roles["viewer"]),
                ]
            },
        )

    assert response.status_code == status.HTTP_200_OK
    statuses = {r["row_key"]: (r["status"], r["error"] and r["error"]["status"]) for r in response.json()["results"]}
    assert statuses == {"deactivated": ("failed", status.HTTP_409_CONFLICT), "invited": ("ok", None)}
    failed = next(r for r in response.json()["results"] if r["row_key"] == "deactivated")
    # The console tells this refusal from "already a member" by its type, never by the English detail.
    assert failed["error"]["type"] == "urn:redteam:error:user-deactivated"


async def test_bulk_add_takes_more_rows_than_an_invitation_bulk(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    # "Add all members" sends a whole organization: this envelope keeps the platform row cap and
    # must not be aligned with the tighter `MAX_INVITE_ROWS`, which exists because invites send mail.
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    rows = [_row(str(i), uuid4(), system_roles["viewer"]) for i in range(MAX_INVITE_ROWS + 1)]

    with as_user(owner):
        response = await async_client_with_db.post(_bulk_url(group), json={"rows": rows, "dry_run": True})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["total"] == MAX_INVITE_ROWS + 1


async def test_bulk_add_on_a_hidden_group_is_404(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    manager = await _user(db_session, permissions=[Permission.EVALUATION_GROUPS_MANAGE_MEMBERS.value, *_READ])
    target = await _user(db_session)

    with as_user(manager):
        response = await async_client_with_db.post(
            _bulk_url(group), json={"rows": [_row("a", target.id, system_roles["viewer"])]}
        )

    assert response.status_code == status.HTTP_404_NOT_FOUND


async def test_bulk_add_needs_manage_members(
    async_client_with_db: AsyncClient, db_session: AsyncSession, system_roles: dict[str, Role]
) -> None:
    owner = await _user(db_session)
    group = await _owned_group(db_session, system_roles, owner)
    member = await _user(db_session)
    await _assign(db_session, group, member, system_roles["red_teamer"])
    target = await _user(db_session)

    with as_user(member):
        response = await async_client_with_db.post(
            _bulk_url(group), json={"rows": [_row("a", target.id, system_roles["viewer"])]}
        )

    assert response.status_code == status.HTTP_403_FORBIDDEN
