"""Integration tests for the self-service /api/v1/auth/me write endpoints."""

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest
import time_machine
from fastapi import status
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.audit.enums import AuditAction
from app.core.audit.models import AuditLog
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.services.passwords import verify_password
from app.core.auth.services.users import create_user
from app.core.terms.service import publish_terms
from tests.api.v1.conftest import PROBLEM_CT
from tests.api.v1.conftest import bearer


async def _account(db: AsyncSession, *, first_name: str | None = None, last_name: str | None = None) -> User:
    role = Role(name=f"self-{uuid4().hex[:8]}", permissions=["flags:read"], is_system=False)
    db.add(role)
    await db.flush()
    return await create_user(
        db,
        email=f"self-{uuid4().hex[:8]}@example.com",
        first_name=first_name,
        last_name=last_name,
        roles=[role],
    )


@pytest.mark.integration
async def test_patch_me_sets_names(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _account(db_session)

    response = await auth_db_client.patch(
        "/api/v1/auth/me",
        headers=bearer(user),
        json={"first_name": "Ada", "last_name": "Lovelace"},
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["first_name"] == "Ada"
    assert body["last_name"] == "Lovelace"
    await db_session.refresh(user)
    assert (user.first_name, user.last_name) == ("Ada", "Lovelace")


@pytest.mark.integration
async def test_patch_me_clears_explicit_null_and_keeps_omitted(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _account(db_session, first_name="Ada", last_name="Lovelace")

    response = await auth_db_client.patch("/api/v1/auth/me", headers=bearer(user), json={"last_name": None})

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["first_name"] == "Ada"
    assert body["last_name"] is None


@pytest.mark.integration
async def test_patch_me_is_audited_with_the_changed_fields(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _account(db_session, first_name="Ada")

    response = await auth_db_client.patch("/api/v1/auth/me", headers=bearer(user), json={"first_name": "Augusta"})

    assert response.status_code == status.HTTP_200_OK
    result = await db_session.execute(
        select(AuditLog).where(
            col(AuditLog.action) == AuditAction.USER_UPDATE.value, col(AuditLog.object_id) == user.id
        )
    )
    row = result.scalar_one()
    assert row.actor_id == user.id
    assert row.before == {"first_name": "Ada"}
    assert row.after == {"first_name": "Augusta"}


@pytest.mark.integration
async def test_patch_me_cannot_touch_roles(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    # `role_ids` is the admin PATCH's field; smuggling it here must change nothing.
    user = await _account(db_session)
    role_names = {role.name for role in user.roles}

    response = await auth_db_client.patch(
        "/api/v1/auth/me",
        headers=bearer(user),
        json={"first_name": "Ada", "role_ids": [str(uuid4())]},
    )

    assert response.status_code == status.HTTP_200_OK
    assert {r["name"] for r in response.json()["roles"]} == role_names


@pytest.mark.integration
async def test_patch_me_requires_auth(auth_db_client: AsyncClient) -> None:
    response = await auth_db_client.patch("/api/v1/auth/me", json={"first_name": "Ada"})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


_CURRENT = "original-pass-123"
_NEW = "brand-new-pass-456"


async def _local_account(db: AsyncSession, *, password: str | None = _CURRENT) -> User:
    role = Role(name=f"self-{uuid4().hex[:8]}", permissions=["flags:read"], is_system=False)
    db.add(role)
    await db.flush()
    return await create_user(
        db,
        email=f"self-{uuid4().hex[:8]}@example.com",
        first_name="Casey",
        password=SecretStr(password) if password is not None else None,
        status=UserStatus.ACTIVE,
        roles=[role],
    )


@pytest.mark.integration
async def test_change_password_rotates_credential_and_revokes_sessions(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _local_account(db_session)
    old_headers = bearer(user)

    response = await auth_db_client.post(
        "/api/v1/auth/me/password",
        headers=old_headers,
        json={"current_password": _CURRENT, "password": _NEW},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    await db_session.refresh(user)
    assert verify_password(_NEW, user.password or "")
    assert not verify_password(_CURRENT, user.password or "")
    # Every pre-change session is dead, this one included.
    revoked = await auth_db_client.get("/api/v1/auth/me", headers=old_headers)
    assert revoked.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.integration
async def test_change_password_is_audited(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _local_account(db_session)

    response = await auth_db_client.post(
        "/api/v1/auth/me/password",
        headers=bearer(user),
        json={"current_password": _CURRENT, "password": _NEW},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    result = await db_session.execute(
        select(AuditLog).where(
            col(AuditLog.action) == AuditAction.AUTH_CREDENTIAL_CHANGED.value, col(AuditLog.object_id) == user.id
        )
    )
    row = result.scalar_one()
    assert row.actor_id == user.id
    assert row.before is None
    assert row.after is None


@pytest.mark.integration
async def test_change_password_wrong_current_is_a_field_error_not_a_401(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # A 401 would make the FE drop the session over a typo — the client treats
    # every 401 as an expired token.
    user = await _local_account(db_session)

    response = await auth_db_client.post(
        "/api/v1/auth/me/password",
        headers=bearer(user),
        json={"current_password": "not-the-password", "password": _NEW},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"] == PROBLEM_CT
    error = response.json()["errors"][0]
    assert error["loc"][-1] == "current_password"
    assert error["type"] == "current_password_incorrect"


@pytest.mark.integration
async def test_change_password_missing_current_is_422(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _local_account(db_session)

    response = await auth_db_client.post("/api/v1/auth/me/password", headers=bearer(user), json={"password": _NEW})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_change_password_passwordless_account_is_409(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # Pins the response contract; the branch variants (pure-IdP vs OIDC-cleared)
    # live in the service mirror (tests/core/auth/services/test_account.py).
    user = await _local_account(db_session, password=None)

    response = await auth_db_client.post(
        "/api/v1/auth/me/password",
        headers=bearer(user),
        json={"current_password": _CURRENT, "password": _NEW},
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"] == PROBLEM_CT


@pytest.mark.integration
async def test_change_password_below_absolute_minimum_is_422(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _local_account(db_session)

    response = await auth_db_client.post(
        "/api/v1/auth/me/password", headers=bearer(user), json={"current_password": _CURRENT, "password": "short12"}
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_patch_me_noop_still_writes_an_audit_row(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    # Deliberate parity with the admin PATCH (platform-settings has the opposite rule).
    user = await _account(db_session, first_name="Ada")

    response = await auth_db_client.patch("/api/v1/auth/me", headers=bearer(user), json={"first_name": "Ada"})

    assert response.status_code == status.HTTP_200_OK
    result = await db_session.execute(
        select(AuditLog).where(
            col(AuditLog.action) == AuditAction.USER_UPDATE.value, col(AuditLog.object_id) == user.id
        )
    )
    row = result.scalar_one()
    assert row.before == {}
    assert row.after == {}


@pytest.mark.integration
async def test_me_writes_return_404_once_the_account_is_soft_deleted(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # A soft-delete revokes nothing, so a live token reaches every write and must get the declared 404.
    user = await _local_account(db_session)
    headers = bearer(user)
    document = await publish_terms(db_session, version="1.0", content="Terms")
    user.deleted_at = datetime.now(UTC)
    db_session.add(user)
    await db_session.flush()

    patched = await auth_db_client.patch("/api/v1/auth/me", headers=headers, json={"first_name": "Ghost"})
    changed = await auth_db_client.post(
        "/api/v1/auth/me/password", headers=headers, json={"current_password": _CURRENT, "password": _NEW}
    )
    accepted = await auth_db_client.post("/api/v1/auth/me/terms", headers=headers, json={"terms_id": str(document.id)})

    for response in (patched, changed, accepted):
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.headers["content-type"] == PROBLEM_CT


@pytest.mark.integration
async def test_get_me_reports_no_consent_while_nothing_is_published(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _account(db_session)

    response = await auth_db_client.get("/api/v1/auth/me", headers=bearer(user))

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["consent_terms"] is False
    assert body["consent_emails"] is False
    assert body["accepted_terms"] is None
    assert body["terms_accepted_at"] is None
    assert body["terms_acceptance_required"] is False


@pytest.mark.integration
async def test_get_me_requires_acceptance_once_a_version_is_published(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _account(db_session)
    await publish_terms(db_session, version="1.0", content="Terms")

    response = await auth_db_client.get("/api/v1/auth/me", headers=bearer(user))

    assert response.json()["terms_acceptance_required"] is True


@pytest.mark.integration
async def test_post_me_terms_records_acceptance_and_clears_the_gate(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _account(db_session)
    document = await publish_terms(db_session, version="1.0", content="Terms")

    response = await auth_db_client.post(
        "/api/v1/auth/me/terms", headers=bearer(user), json={"terms_id": str(document.id)}
    )

    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["consent_terms"] is True
    assert body["terms_acceptance_required"] is False
    assert body["terms_accepted_at"] is not None
    assert body["accepted_terms"]["version"] == "1.0"
    await db_session.refresh(user)
    assert user.accepted_terms_id == document.id
    result = await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.TERMS_ACCEPT.value))
    assert result.scalar_one().after == {"version": "1.0"}


@pytest.mark.integration
async def test_get_me_projects_the_version_accepted_not_the_current_one(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A client pairing `terms_accepted_at` with the *current* version would state something false."""
    user = await _account(db_session)
    with time_machine.travel(datetime(2026, 1, 1, tzinfo=UTC), tick=False):
        accepted = await publish_terms(db_session, version="1.0", content="First")
    await auth_db_client.post("/api/v1/auth/me/terms", headers=bearer(user), json={"terms_id": str(accepted.id)})
    with time_machine.travel(datetime(2026, 6, 1, tzinfo=UTC), tick=False):
        await publish_terms(db_session, version="2.0", content="Second")

    body = (await auth_db_client.get("/api/v1/auth/me", headers=bearer(user))).json()

    assert body["accepted_terms"]["version"] == "1.0"
    assert body["accepted_terms"]["id"] == str(accepted.id)
    assert body["terms_acceptance_required"] is True


@pytest.mark.integration
async def test_get_me_reports_no_accepted_version_once_it_is_tombstoned(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The documented second null: `accepted_terms` drops out while the timestamp stays."""
    user = await _account(db_session)
    accepted = await publish_terms(db_session, version="1.0", content="First")
    await auth_db_client.post("/api/v1/auth/me/terms", headers=bearer(user), json={"terms_id": str(accepted.id)})
    accepted.deleted_at = datetime.now(UTC)
    db_session.add(accepted)
    await db_session.flush()

    body = (await auth_db_client.get("/api/v1/auth/me", headers=bearer(user))).json()

    assert body["accepted_terms"] is None
    assert body["terms_accepted_at"] is not None
    assert body["consent_terms"] is True


@pytest.mark.integration
async def test_post_me_terms_refuses_a_superseded_version(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _account(db_session)
    with time_machine.travel(datetime(2026, 1, 1, tzinfo=UTC), tick=False):
        stale = await publish_terms(db_session, version="1.0", content="First")
    with time_machine.travel(datetime(2026, 6, 1, tzinfo=UTC), tick=False):
        await publish_terms(db_session, version="2.0", content="Second")

    response = await auth_db_client.post(
        "/api/v1/auth/me/terms", headers=bearer(user), json={"terms_id": str(stale.id)}
    )

    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.headers["content-type"] == PROBLEM_CT


@pytest.mark.integration
async def test_post_me_terms_audits_a_repeat_submit_only_once(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _account(db_session)
    document = await publish_terms(db_session, version="1.0", content="Terms")
    body = {"terms_id": str(document.id)}

    first = await auth_db_client.post("/api/v1/auth/me/terms", headers=bearer(user), json=body)
    second = await auth_db_client.post("/api/v1/auth/me/terms", headers=bearer(user), json=body)

    assert (first.status_code, second.status_code) == (status.HTTP_200_OK, status.HTTP_200_OK)
    assert first.json()["terms_accepted_at"] == second.json()["terms_accepted_at"]
    result = await db_session.execute(select(AuditLog).where(col(AuditLog.action) == AuditAction.TERMS_ACCEPT.value))
    assert len(result.scalars().all()) == 1


@pytest.mark.integration
async def test_post_me_terms_needs_a_token(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    document = await publish_terms(db_session, version="1.0", content="Terms")

    response = await auth_db_client.post("/api/v1/auth/me/terms", json={"terms_id": str(document.id)})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.integration
async def test_patch_me_sets_the_email_consent_flag(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _account(db_session)

    response = await auth_db_client.patch("/api/v1/auth/me", headers=bearer(user), json={"consent_emails": True})

    assert response.status_code == status.HTTP_200_OK
    assert response.json()["consent_emails"] is True
    await db_session.refresh(user)
    assert user.consent_emails is True


@pytest.mark.integration
async def test_patch_me_explicit_null_email_consent_is_422(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await _account(db_session)

    response = await auth_db_client.patch("/api/v1/auth/me", headers=bearer(user), json={"consent_emails": None})

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_patch_me_audits_a_consent_change(auth_db_client: AsyncClient, db_session: AsyncSession) -> None:
    user = await _account(db_session)

    response = await auth_db_client.patch("/api/v1/auth/me", headers=bearer(user), json={"consent_emails": True})

    assert response.status_code == status.HTTP_200_OK
    result = await db_session.execute(
        select(AuditLog).where(
            col(AuditLog.action) == AuditAction.USER_UPDATE.value, col(AuditLog.object_id) == user.id
        )
    )
    row = result.scalar_one()
    assert row.before == {"consent_emails": False}
    assert row.after == {"consent_emails": True}
