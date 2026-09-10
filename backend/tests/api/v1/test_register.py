"""Integration tests for `/v1/auth/register` and `/v1/auth/register/verify`."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import status
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import EmailVerification
from app.core.auth.models import EmailVerificationStatus
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.services.passwords import verify_password
from app.core.auth.services.registration import register_user
from app.core.auth.services.users import create_user as create_user_service
from app.core.auth.services.users import get_user_by_email
from app.core.email.models import OutboundEmail
from app.core.platform_settings.service import update_platform_settings
from app.core.terms.service import publish_terms


@pytest.fixture(autouse=True)
def enqueue_stub(celery_enqueue_stub: MagicMock) -> MagicMock:
    """Module-wide: every path here dispatches mail. Aliases the shared spy under this module's name."""
    return celery_enqueue_stub


@pytest_asyncio.fixture
async def default_role(db_session: AsyncSession) -> Role:
    role = Role(name="red_teamer", display_name="Red Teamer", description="Red Teamer", permissions=[], is_default=True)
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


async def _outbound_emails(session: AsyncSession) -> list[OutboundEmail]:
    result = await session.execute(select(OutboundEmail).order_by(col(OutboundEmail.created_at)))
    return list(result.scalars().all())


async def _get_user(session: AsyncSession, email: str) -> User:
    user = await get_user_by_email(session, email)
    assert user is not None, f"user with email {email!r} not found"
    return user


# ---------------------------------------------------------------------------
# POST /api/v1/auth/register
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_register_new_email_returns_202_and_creates_pending_user(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={
            "email": "ada@example.com",
            "password": "supersecret-12345",
            "first_name": "Ada",
            "last_name": "Lovelace",
        },
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.content == b""

    user = await _get_user(db_session, "ada@example.com")
    assert user.status is UserStatus.PENDING
    assert user.email_verified_at is None
    assert user.first_name == "Ada"
    assert user.last_name == "Lovelace"
    assert user.password is not None
    assert verify_password("supersecret-12345", user.password)
    assert [r.name for r in user.roles] == ["red_teamer"]


@pytest.mark.integration
async def test_resend_returns_202_regardless_of_account_state(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    # Wiring + contract: unknown email gets the same 202/empty body a pending one would.
    response = await auth_db_client.post(
        "/api/v1/auth/register/resend",
        json={"email": "ghost@example.com"},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert response.content == b""


@pytest.mark.integration
async def test_register_returns_403_when_invite_only(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    await update_platform_settings(db_session, invite_only=True)

    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "supersecret-12345"},
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == status.HTTP_403_FORBIDDEN
    assert body["title"] == "Forbidden"
    assert await get_user_by_email(db_session, "ada@example.com") is None


@pytest.mark.integration
async def test_register_normalizes_email(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "Ada@Example.COM", "password": "supersecret-12345"},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    user = await _get_user(db_session, "ada@example.com")
    assert user.email == "ada@example.com"


@pytest.mark.integration
async def test_register_enqueues_verification_email(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "supersecret-12345"},
    )

    emails = await _outbound_emails(db_session)
    assert len(emails) == 1
    assert emails[0].template_name == "email_verification"
    assert emails[0].recipient == "ada@example.com"
    # The token URL is a bearer secret — kept off the persisted row, carried on
    # the task signature instead.
    assert "verify_url" not in emails[0].context
    secret = enqueue_stub.call_args.kwargs["args"][1]
    assert "http://localhost:3000/register/verify?token=" in secret["verify_url"]


@pytest.mark.integration
async def test_register_inserts_pending_verification_row(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "supersecret-12345"},
    )

    rows = (await db_session.execute(EmailVerification.live_select())).scalars().all()
    assert len(rows) == 1
    assert rows[0].status is EmailVerificationStatus.PENDING
    assert len(rows[0].token_hash) == 64


@pytest.mark.integration
async def test_register_active_email_is_silent_no_op(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    """Anti-enumeration: an already-active email gets the same 202 with no DB write."""
    await create_user_service(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        email_verified=True,
        status=UserStatus.ACTIVE,
        roles=[default_role],
    )
    before = len(await _outbound_emails(db_session))

    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "different-pass-12345"},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    assert len(await _outbound_emails(db_session)) == before
    assert (await db_session.execute(EmailVerification.live_select())).scalars().all() == []


@pytest.mark.integration
async def test_register_invited_email_reissues_invitation(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    """An invited account self-registering is re-issued its invitation; no password is bound here.

    The submitted password is ignored — the email owner sets the credential via
    the accept link. So the account stays INVITED and passwordless, and the
    onboarding mail is a platform invitation, not an email verification.
    """
    user = await create_user_service(
        db_session,
        email="ada@example.com",
        status=UserStatus.INVITED,
        roles=[default_role],
    )

    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "attacker-pass-12345"},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    emails = await _outbound_emails(db_session)
    assert len(emails) == 1
    assert emails[0].template_name == "platform_invitation"
    # No email-verification token is minted; onboarding rides the invitation accept link.
    assert (await db_session.execute(EmailVerification.live_select())).scalars().all() == []

    await db_session.refresh(user)
    assert user.status is UserStatus.INVITED
    assert user.password is None


@pytest.mark.integration
async def test_register_pending_email_reissues_verification(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    """Re-registering a pending user revokes the prior token, issues a new one, and sends mail.

    The stored password is *not* overwritten — re-registration can't hijack a half-completed signup.
    """
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )
    user_before = await _get_user(db_session, "ada@example.com")
    original_hash = user_before.password
    assert original_hash is not None

    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "different-pass-67890"},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED

    user_after = await _get_user(db_session, "ada@example.com")
    assert user_after.password == original_hash, "password must not be overwritten on re-register"

    rows = (await db_session.execute(EmailVerification.live_select())).scalars().all()
    by_status = {row.status: row for row in rows}
    assert EmailVerificationStatus.REVOKED in by_status
    assert EmailVerificationStatus.PENDING in by_status
    assert by_status[EmailVerificationStatus.REVOKED].revoked_at is not None

    emails = await _outbound_emails(db_session)
    assert len(emails) == 2  # two verification emails total


@pytest.mark.integration
async def test_register_short_password_returns_422(
    auth_db_client: AsyncClient,
    default_role: Role,
) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "short"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_register_password_over_cap_returns_422(
    auth_db_client: AsyncClient,
    default_role: Role,
) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "x" * 129},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


@pytest.mark.integration
async def test_register_common_password_returns_400(
    auth_db_client: AsyncClient,
    default_role: Role,
) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "password1234"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"] == "application/problem+json"
    # Field-addressable: errors[] points at the password field with a machine code.
    error = response.json()["errors"][0]
    assert error["loc"][-1] == "password"
    assert error["type"] == "password_too_common"


@pytest.mark.integration
async def test_register_honours_the_configured_minimum_length(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    # Above the schema floor, so it is the platform setting — not the contract — doing the work,
    # and the rejection is a 400 with a code rather than the schema's bare 422.
    await update_platform_settings(db_session, password_min_length=20)

    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "uncommon-42"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    error = response.json()["errors"][0]
    assert error["loc"][-1] == "password"
    assert error["type"] == "password_too_short"


@pytest.mark.integration
async def test_register_password_similar_to_email_returns_400(
    auth_db_client: AsyncClient,
    default_role: Role,
) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada.lovelace@example.com", "password": "ada.lovelace@example.com"},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["errors"][0]["type"] == "password_too_similar"


@pytest.mark.integration
async def test_register_invalid_email_returns_422(
    auth_db_client: AsyncClient,
    default_role: Role,
) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": "supersecret-12345"},
    )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


# ---------------------------------------------------------------------------
# POST /api/v1/auth/register/verify
# ---------------------------------------------------------------------------


async def _register_and_extract_token(
    session: AsyncSession,
    enqueue: MagicMock,
    *,
    email: str = "ada@example.com",
    password: str = "supersecret-12345",  # noqa: S107
    first_name: str | None = "Ada",
    last_name: str | None = None,
) -> str:
    await register_user(
        session,
        email=email,
        password=SecretStr(password),
        first_name=first_name,
        last_name=last_name,
    )
    # The token rides the task signature (`secret_context`), not the persisted row.
    for call in enqueue.call_args_list:
        secret = call.kwargs["args"][1]
        if "verify_url" in secret:
            return secret["verify_url"].rsplit("token=", 1)[-1]
    raise AssertionError("no verification email was enqueued")


@pytest.mark.integration
async def test_verify_happy_path_returns_204_and_activates_user(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    token = await _register_and_extract_token(db_session, enqueue_stub)

    response = await auth_db_client.post(
        "/api/v1/auth/register/verify",
        json={"token": token},
    )

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert response.content == b""

    user = await _get_user(db_session, "ada@example.com")
    assert user.status is UserStatus.ACTIVE
    assert user.email_verified_at is not None

    rows = (await db_session.execute(EmailVerification.live_select())).scalars().all()
    assert len(rows) == 1
    assert rows[0].status is EmailVerificationStatus.VERIFIED
    assert rows[0].verified_at is not None


@pytest.mark.integration
async def test_verify_sends_account_activated_email(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    token = await _register_and_extract_token(db_session, enqueue_stub)

    await auth_db_client.post("/api/v1/auth/register/verify", json={"token": token})

    emails = await _outbound_emails(db_session)
    template_names = sorted(e.template_name for e in emails)
    assert template_names == ["account_activated", "email_verification"]
    activated = next(e for e in emails if e.template_name == "account_activated")
    assert activated.recipient == "ada@example.com"
    assert activated.context["user_name"] == "Ada"


@pytest.mark.integration
async def test_verify_unknown_token_returns_404(auth_db_client: AsyncClient) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/register/verify",
        json={"token": "no-such-token"},
    )

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.integration
async def test_verify_second_call_returns_410(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    token = await _register_and_extract_token(db_session, enqueue_stub)
    await auth_db_client.post("/api/v1/auth/register/verify", json={"token": token})

    response = await auth_db_client.post("/api/v1/auth/register/verify", json={"token": token})

    assert response.status_code == status.HTTP_410_GONE
    assert "verified" in response.json()["detail"]


@pytest.mark.integration
async def test_verify_expired_token_returns_410(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    """Overdue PENDING surfaces as EXPIRED — the no-DB-write assertion lives in the service test."""
    token = await _register_and_extract_token(db_session, enqueue_stub)
    row = (await db_session.execute(EmailVerification.live_select())).scalars().one()
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.add(row)
    await db_session.commit()

    response = await auth_db_client.post("/api/v1/auth/register/verify", json={"token": token})

    assert response.status_code == status.HTTP_410_GONE
    assert "expired" in response.json()["detail"]


@pytest.mark.integration
async def test_verify_revoked_token_returns_410(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    """Re-registering revokes the first token; using it after that yields 410."""
    first_token = await _register_and_extract_token(db_session, enqueue_stub)
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )

    response = await auth_db_client.post(
        "/api/v1/auth/register/verify",
        json={"token": first_token},
    )

    assert response.status_code == status.HTTP_410_GONE
    assert "revoked" in response.json()["detail"]


@pytest.mark.integration
async def test_register_declining_published_terms_is_a_field_error(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    await publish_terms(db_session, version="1.0", content="Terms")

    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "supersecret-12345", "consent_terms": False},
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    error = response.json()["errors"][0]
    assert error["loc"][-1] == "consent_terms"
    assert error["type"] == "consent_terms_required"


@pytest.mark.integration
async def test_register_accepting_terms_records_the_consent(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    document = await publish_terms(db_session, version="1.0", content="Terms")

    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={
            "email": "ada@example.com",
            "password": "supersecret-12345",
            "consent_terms": True,
            "consent_emails": True,
            "terms_id": str(document.id),
        },
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    user = await _get_user(db_session, "ada@example.com")
    assert user.accepted_terms_id == document.id
    assert user.consent_emails is True


@pytest.mark.integration
async def test_register_without_consent_is_accepted_while_no_terms_exist(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={"email": "ada@example.com", "password": "supersecret-12345"},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED
    user = await _get_user(db_session, "ada@example.com")
    assert user.accepted_terms_id is None


@pytest.mark.integration
async def test_register_with_a_stale_terms_version_is_409(
    auth_db_client: AsyncClient,
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    await publish_terms(db_session, version="1.0", content="Terms")

    response = await auth_db_client.post(
        "/api/v1/auth/register",
        json={
            "email": "ada@example.com",
            "password": "supersecret-12345",
            "consent_terms": True,
            "terms_id": str(uuid4()),
        },
    )

    assert response.status_code == status.HTTP_409_CONFLICT
