"""Integration tests for the `/v1/organizations` router."""

from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.roles import Permission
from app.core.auth.services.users import create_user as create_user_service
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import EvaluationGroup
from app.core.organizations.models import Organization
from tests.api.v1.conftest import bearer as _auth
from tests.api.v1.conftest import make_role

ORG_PERMS = [
    Permission.ORGANIZATIONS_READ.value,
    Permission.ORGANIZATIONS_CREATE.value,
    Permission.ORGANIZATIONS_UPDATE.value,
    Permission.ORGANIZATIONS_DELETE.value,
    Permission.ORGANIZATIONS_MANAGE_MEMBERS.value,
]


async def _user(db: AsyncSession, *, email: str, permissions: list[str]) -> User:
    role = await make_role(db, permissions)
    return await create_user_service(db, email=email, roles=[role], status=UserStatus.ACTIVE, email_verified=True)


@pytest_asyncio.fixture
async def admin(db_session: AsyncSession) -> User:
    return await _user(db_session, email="admin@example.com", permissions=ORG_PERMS)


@pytest_asyncio.fixture
async def reader(db_session: AsyncSession) -> User:
    return await _user(db_session, email="reader@example.com", permissions=[Permission.ORGANIZATIONS_READ.value])


@pytest_asyncio.fixture
async def outsider(db_session: AsyncSession) -> User:
    return await _user(db_session, email="nobody@example.com", permissions=[])


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_create_organization_returns_201(auth_db_client: AsyncClient, admin: User) -> None:
    response = await auth_db_client.post(
        "/api/v1/organizations", json={"name": "Acme Corp", "description": "Tenant"}, headers=_auth(admin)
    )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["name"] == "Acme Corp"
    assert body["description"] == "Tenant"
    assert response.headers["Location"] == f"/api/v1/organizations/{body['id']}"


@pytest.mark.integration
async def test_create_rejects_duplicate_name(auth_db_client: AsyncClient, admin: User) -> None:
    await auth_db_client.post("/api/v1/organizations", json={"name": "Dup"}, headers=_auth(admin))

    response = await auth_db_client.post("/api/v1/organizations", json={"name": "Dup"}, headers=_auth(admin))

    assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.integration
async def test_create_unauthenticated_returns_401(auth_db_client: AsyncClient) -> None:
    response = await auth_db_client.post("/api/v1/organizations", json={"name": "Nope"})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.integration
async def test_list_and_filter_by_name(auth_db_client: AsyncClient, admin: User) -> None:
    for name in ("Alpha", "Beta", "Alphabet"):
        await auth_db_client.post("/api/v1/organizations", json={"name": name}, headers=_auth(admin))

    response = await auth_db_client.get("/api/v1/organizations", params={"name": "Alpha"}, headers=_auth(admin))

    assert response.status_code == status.HTTP_200_OK
    names = {item["name"] for item in response.json()["items"]}
    assert names == {"Alpha", "Alphabet"}


@pytest.mark.integration
async def test_list_order_by_name_descending(auth_db_client: AsyncClient, admin: User) -> None:
    for name in ("Alpha", "Charlie", "Bravo"):
        await auth_db_client.post("/api/v1/organizations", json={"name": name}, headers=_auth(admin))

    response = await auth_db_client.get("/api/v1/organizations", params={"order_by": "-name"}, headers=_auth(admin))

    assert response.status_code == status.HTTP_200_OK
    assert [item["name"] for item in response.json()["items"]] == ["Charlie", "Bravo", "Alpha"]


@pytest.mark.integration
async def test_list_rejects_unknown_order_by(auth_db_client: AsyncClient, admin: User) -> None:
    response = await auth_db_client.get("/api/v1/organizations", params={"order_by": "bogus"}, headers=_auth(admin))

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_get_organization(auth_db_client: AsyncClient, admin: User) -> None:
    created = (await auth_db_client.post("/api/v1/organizations", json={"name": "Solo"}, headers=_auth(admin))).json()

    response = await auth_db_client.get(f"/api/v1/organizations/{created['id']}", headers=_auth(admin))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == created["id"]


@pytest.mark.integration
async def test_get_unknown_returns_404(auth_db_client: AsyncClient, admin: User) -> None:
    response = await auth_db_client.get(
        "/api/v1/organizations/7c9e6679-7425-40de-944b-e07fc1f90ae7", headers=_auth(admin)
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_update_organization(auth_db_client: AsyncClient, admin: User) -> None:
    created = (
        await auth_db_client.post(
            "/api/v1/organizations", json={"name": "Old", "description": "keep"}, headers=_auth(admin)
        )
    ).json()

    response = await auth_db_client.patch(
        f"/api/v1/organizations/{created['id']}", json={"name": "New"}, headers=_auth(admin)
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["name"] == "New"
    assert body["description"] == "keep"  # omitted field left unchanged


@pytest.mark.integration
async def test_update_clears_description_with_explicit_null(auth_db_client: AsyncClient, admin: User) -> None:
    created = (
        await auth_db_client.post(
            "/api/v1/organizations", json={"name": "Org", "description": "drop me"}, headers=_auth(admin)
        )
    ).json()

    response = await auth_db_client.patch(
        f"/api/v1/organizations/{created['id']}", json={"description": None}, headers=_auth(admin)
    )

    assert response.json()["description"] is None


@pytest.mark.integration
async def test_delete_then_get_is_404(auth_db_client: AsyncClient, admin: User) -> None:
    created = (await auth_db_client.post("/api/v1/organizations", json={"name": "Doomed"}, headers=_auth(admin))).json()

    deleted = await auth_db_client.delete(f"/api/v1/organizations/{created['id']}", headers=_auth(admin))
    fetched = await auth_db_client.get(f"/api/v1/organizations/{created['id']}", headers=_auth(admin))

    assert deleted.status_code == status.HTTP_204_NO_CONTENT
    assert fetched.status_code == status.HTTP_404_NOT_FOUND


def _group_referencing(org_id: str, *, created_by_id: UUID) -> EvaluationGroup:
    return EvaluationGroup(
        title="Engagement",
        description="references the org",
        created_by_id=created_by_id,
        access_level=EvaluationGroupAccessLevel.ORGANIZATION,
        organization_id=UUID(org_id),
        status=PublicationStatus.PUBLISHED,
        start_date=date(2026, 3, 1),
    )


@pytest.mark.integration
async def test_delete_blocked_while_group_references_org(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    # A soft-delete doesn't fire the FK `SET NULL`, so an org still referenced by a
    # live group is blocked (409) — else the group would go dark and un-editable.
    created = (await auth_db_client.post("/api/v1/organizations", json={"name": "In Use"}, headers=_auth(admin))).json()
    db_session.add(_group_referencing(created["id"], created_by_id=admin.id))
    await db_session.flush()

    response = await auth_db_client.delete(f"/api/v1/organizations/{created['id']}", headers=_auth(admin))

    assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.integration
async def test_delete_succeeds_when_only_a_soft_deleted_group_references_org(
    auth_db_client: AsyncClient, db_session: AsyncSession, admin: User
) -> None:
    # The reference count is live-only: a soft-deleted group no longer blocks the org.
    created = (
        await auth_db_client.post("/api/v1/organizations", json={"name": "Free Soon"}, headers=_auth(admin))
    ).json()
    group = _group_referencing(created["id"], created_by_id=admin.id)
    db_session.add(group)
    await db_session.flush()
    group.soft_delete(None)
    await db_session.flush()

    response = await auth_db_client.delete(f"/api/v1/organizations/{created['id']}", headers=_auth(admin))

    assert response.status_code == status.HTTP_204_NO_CONTENT


async def _backdate_org_tombstone(db_session: AsyncSession, org_id: str, *, days: int) -> None:
    """Age a tombstone past the restore window.

    Plain `select` (it must see deleted rows) with `populate_existing` to refresh just this
    row — a session-wide `expire_all()` would leave every `User` the test still holds
    expired, and the next attribute read would try a sync lazy-load off the async session.
    """
    statement = (
        select(Organization).where(col(Organization.id) == UUID(org_id)).execution_options(populate_existing=True)
    )
    row = (await db_session.execute(statement)).scalar_one()
    row.deleted_at = datetime.now(UTC) - timedelta(days=days)
    db_session.add(row)
    await db_session.flush()


@pytest.mark.integration
async def test_restore_returns_the_organization(auth_db_client: AsyncClient, admin: User) -> None:
    created = (await auth_db_client.post("/api/v1/organizations", json={"name": "Back"}, headers=_auth(admin))).json()
    await auth_db_client.delete(f"/api/v1/organizations/{created['id']}", headers=_auth(admin))

    response = await auth_db_client.post(f"/api/v1/organizations/{created['id']}/restore", headers=_auth(admin))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted_at"] is None
    fetched = await auth_db_client.get(f"/api/v1/organizations/{created['id']}", headers=_auth(admin))
    assert fetched.status_code == status.HTTP_200_OK


@pytest.mark.integration
async def test_restore_brings_members_back_with_it(
    auth_db_client: AsyncClient, admin: User, db_session: AsyncSession
) -> None:
    # The delete leaves `User.organization_id` pointing at the tombstone (members fail
    # closed to orgless meanwhile), so reviving the row re-resolves them — no re-attach.
    org = (await auth_db_client.post("/api/v1/organizations", json={"name": "Staffed"}, headers=_auth(admin))).json()
    target = await _user(db_session, email="restored-member@example.com", permissions=[])
    await auth_db_client.post(
        f"/api/v1/organizations/{org['id']}/members", json={"user_id": str(target.id)}, headers=_auth(admin)
    )
    await auth_db_client.delete(f"/api/v1/organizations/{org['id']}", headers=_auth(admin))

    assert (
        await auth_db_client.post(f"/api/v1/organizations/{org['id']}/restore", headers=_auth(admin))
    ).status_code == status.HTTP_200_OK

    members = await auth_db_client.get(f"/api/v1/organizations/{org['id']}/members", headers=_auth(admin))
    assert {m["id"] for m in members.json()["items"]} == {str(target.id)}


@pytest.mark.integration
async def test_restore_409s_when_a_live_org_took_its_name(auth_db_client: AsyncClient, admin: User) -> None:
    # `name` is unique among live rows only, so the delete freed it for a re-creation.
    created = (await auth_db_client.post("/api/v1/organizations", json={"name": "Reused"}, headers=_auth(admin))).json()
    await auth_db_client.delete(f"/api/v1/organizations/{created['id']}", headers=_auth(admin))
    replacement = await auth_db_client.post("/api/v1/organizations", json={"name": "Reused"}, headers=_auth(admin))
    assert replacement.status_code == status.HTTP_201_CREATED

    response = await auth_db_client.post(f"/api/v1/organizations/{created['id']}/restore", headers=_auth(admin))

    assert response.status_code == status.HTTP_409_CONFLICT


@pytest.mark.integration
async def test_restore_404s_outside_the_window(
    auth_db_client: AsyncClient, admin: User, db_session: AsyncSession
) -> None:
    created = (
        await auth_db_client.post("/api/v1/organizations", json={"name": "Ancient"}, headers=_auth(admin))
    ).json()
    await auth_db_client.delete(f"/api/v1/organizations/{created['id']}", headers=_auth(admin))
    await _backdate_org_tombstone(db_session, created["id"], days=30)

    response = await auth_db_client.post(f"/api/v1/organizations/{created['id']}/restore", headers=_auth(admin))

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_restore_requires_organizations_delete(auth_db_client: AsyncClient, admin: User, reader: User) -> None:
    created = (await auth_db_client.post("/api/v1/organizations", json={"name": "Gated"}, headers=_auth(admin))).json()
    await auth_db_client.delete(f"/api/v1/organizations/{created['id']}", headers=_auth(admin))

    response = await auth_db_client.post(f"/api/v1/organizations/{created['id']}/restore", headers=_auth(reader))

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_deleted_listing_requires_organizations_delete(auth_db_client: AsyncClient, reader: User) -> None:
    # `organizations:read` opens the live list; the tombstone list is the restore surface.
    response = await auth_db_client.get("/api/v1/organizations?deleted=true", headers=_auth(reader))

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_deleted_listing_shows_tombstones_and_keeps_order_by(
    auth_db_client: AsyncClient, admin: User, db_session: AsyncSession
) -> None:
    # Ordering is the caller's: this list has an `order_by`, so the flag doesn't override it.
    other = await _user(db_session, email="other-org-admin@example.com", permissions=ORG_PERMS)
    first = (await auth_db_client.post("/api/v1/organizations", json={"name": "Gone A"}, headers=_auth(admin))).json()
    second = (await auth_db_client.post("/api/v1/organizations", json={"name": "Gone B"}, headers=_auth(admin))).json()
    await auth_db_client.delete(f"/api/v1/organizations/{first['id']}", headers=_auth(admin))
    await auth_db_client.delete(f"/api/v1/organizations/{second['id']}", headers=_auth(other))

    listed = await auth_db_client.get("/api/v1/organizations?deleted=true&order_by=-deleted_at", headers=_auth(admin))

    assert listed.status_code == status.HTTP_200_OK
    items = listed.json()["items"]
    # Both deleters' tombstones — `organizations:delete` restores any of them.
    assert [item["name"] for item in items] == ["Gone B", "Gone A"]
    assert {item["deleted_by_id"] for item in items} == {str(admin.id), str(other.id)}
    live = await auth_db_client.get("/api/v1/organizations", headers=_auth(admin))
    assert {item["name"] for item in live.json()["items"]}.isdisjoint({"Gone A", "Gone B"})


@pytest.mark.integration
async def test_deleted_listing_excludes_tombstones_outside_the_window(
    auth_db_client: AsyncClient, admin: User, db_session: AsyncSession
) -> None:
    created = (await auth_db_client.post("/api/v1/organizations", json={"name": "Stale"}, headers=_auth(admin))).json()
    await auth_db_client.delete(f"/api/v1/organizations/{created['id']}", headers=_auth(admin))
    await _backdate_org_tombstone(db_session, created["id"], days=30)

    listed = await auth_db_client.get("/api/v1/organizations?deleted=true", headers=_auth(admin))

    assert created["id"] not in {item["id"] for item in listed.json()["items"]}


@pytest.mark.integration
async def test_reader_can_list_and_get(auth_db_client: AsyncClient, admin: User, reader: User) -> None:
    created = (
        await auth_db_client.post("/api/v1/organizations", json={"name": "Visible"}, headers=_auth(admin))
    ).json()

    listed = await auth_db_client.get("/api/v1/organizations", headers=_auth(reader))
    fetched = await auth_db_client.get(f"/api/v1/organizations/{created['id']}", headers=_auth(reader))

    assert listed.status_code == status.HTTP_200_OK
    assert created["id"] in {o["id"] for o in listed.json()["items"]}
    assert fetched.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_assign_member_sets_organization(
    auth_db_client: AsyncClient, admin: User, db_session: AsyncSession
) -> None:
    org = (await auth_db_client.post("/api/v1/organizations", json={"name": "Org"}, headers=_auth(admin))).json()
    target = await _user(db_session, email="member@example.com", permissions=[])

    response = await auth_db_client.post(
        f"/api/v1/organizations/{org['id']}/members", json={"user_id": str(target.id)}, headers=_auth(admin)
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["organization"] == {"id": org["id"], "name": "Org"}

    members = await auth_db_client.get(f"/api/v1/organizations/{org['id']}/members", headers=_auth(admin))
    items = members.json()["items"]
    assert {m["id"] for m in items} == {str(target.id)}
    assert items[0]["organization"] == {"id": org["id"], "name": "Org"}


@pytest.mark.integration
async def test_assign_member_moves_from_prior_org(
    auth_db_client: AsyncClient, admin: User, db_session: AsyncSession
) -> None:
    org_a = (await auth_db_client.post("/api/v1/organizations", json={"name": "A"}, headers=_auth(admin))).json()
    org_b = (await auth_db_client.post("/api/v1/organizations", json={"name": "B"}, headers=_auth(admin))).json()
    target = await _user(db_session, email="mover@example.com", permissions=[])

    await auth_db_client.post(
        f"/api/v1/organizations/{org_a['id']}/members", json={"user_id": str(target.id)}, headers=_auth(admin)
    )
    await auth_db_client.post(
        f"/api/v1/organizations/{org_b['id']}/members", json={"user_id": str(target.id)}, headers=_auth(admin)
    )

    members_a = await auth_db_client.get(f"/api/v1/organizations/{org_a['id']}/members", headers=_auth(admin))
    members_b = await auth_db_client.get(f"/api/v1/organizations/{org_b['id']}/members", headers=_auth(admin))

    assert members_a.json()["items"] == []
    assert {m["id"] for m in members_b.json()["items"]} == {str(target.id)}


@pytest.mark.integration
async def test_remove_member(auth_db_client: AsyncClient, admin: User, db_session: AsyncSession) -> None:
    org = (await auth_db_client.post("/api/v1/organizations", json={"name": "Org"}, headers=_auth(admin))).json()
    target = await _user(db_session, email="leaver@example.com", permissions=[])
    await auth_db_client.post(
        f"/api/v1/organizations/{org['id']}/members", json={"user_id": str(target.id)}, headers=_auth(admin)
    )

    removed = await auth_db_client.delete(
        f"/api/v1/organizations/{org['id']}/members/{target.id}", headers=_auth(admin)
    )
    members = await auth_db_client.get(f"/api/v1/organizations/{org['id']}/members", headers=_auth(admin))

    assert removed.status_code == status.HTTP_204_NO_CONTENT
    assert members.json()["items"] == []


@pytest.mark.integration
async def test_remove_non_member_returns_404(
    auth_db_client: AsyncClient, admin: User, db_session: AsyncSession
) -> None:
    org = (await auth_db_client.post("/api/v1/organizations", json={"name": "Org"}, headers=_auth(admin))).json()
    target = await _user(db_session, email="stranger@example.com", permissions=[])

    response = await auth_db_client.delete(
        f"/api/v1/organizations/{org['id']}/members/{target.id}", headers=_auth(admin)
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_assign_unknown_user_returns_404(auth_db_client: AsyncClient, admin: User) -> None:
    org = (await auth_db_client.post("/api/v1/organizations", json={"name": "Org"}, headers=_auth(admin))).json()

    response = await auth_db_client.post(
        f"/api/v1/organizations/{org['id']}/members",
        json={"user_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"},
        headers=_auth(admin),
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_list_members_unknown_org_returns_404(auth_db_client: AsyncClient, admin: User) -> None:
    response = await auth_db_client.get(
        "/api/v1/organizations/7c9e6679-7425-40de-944b-e07fc1f90ae7/members", headers=_auth(admin)
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.integration
async def test_assign_member_requires_manage_members(
    auth_db_client: AsyncClient, reader: User, admin: User, db_session: AsyncSession
) -> None:
    org = (await auth_db_client.post("/api/v1/organizations", json={"name": "Org"}, headers=_auth(admin))).json()
    target = await _user(db_session, email="t@example.com", permissions=[])

    response = await auth_db_client.post(
        f"/api/v1/organizations/{org['id']}/members", json={"user_id": str(target.id)}, headers=_auth(reader)
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.integration
async def test_me_reflects_assigned_organization(
    auth_db_client: AsyncClient, admin: User, db_session: AsyncSession
) -> None:
    org = (await auth_db_client.post("/api/v1/organizations", json={"name": "Org"}, headers=_auth(admin))).json()
    target = await _user(db_session, email="whoami@example.com", permissions=[])
    await auth_db_client.post(
        f"/api/v1/organizations/{org['id']}/members", json={"user_id": str(target.id)}, headers=_auth(admin)
    )

    # Target's token was minted before assignment; /auth/me resolves org live from the DB.
    response = await auth_db_client.get("/api/v1/auth/me", headers=_auth(target))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["organization"] == {"id": org["id"], "name": "Org"}


@pytest.mark.integration
async def test_me_organization_is_null_when_org_soft_deleted(
    auth_db_client: AsyncClient, admin: User, db_session: AsyncSession
) -> None:
    org = (await auth_db_client.post("/api/v1/organizations", json={"name": "Org"}, headers=_auth(admin))).json()
    target = await _user(db_session, email="orphan@example.com", permissions=[])
    await auth_db_client.post(
        f"/api/v1/organizations/{org['id']}/members", json={"user_id": str(target.id)}, headers=_auth(admin)
    )

    # Soft-delete the org; the user's FK still points at it, but it must read as no org.
    await auth_db_client.delete(f"/api/v1/organizations/{org['id']}", headers=_auth(admin))

    response = await auth_db_client.get("/api/v1/auth/me", headers=_auth(target))

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["organization"] is None
