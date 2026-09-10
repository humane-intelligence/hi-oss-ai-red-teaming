"""Integration tests for `app.core.auth.services.registration`."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import MagicMock
from uuid import UUID
from uuid import uuid4

import pytest
import pytest_asyncio
import time_machine
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

import app.core.email as email_module
from app.core.auth.models import EmailVerification
from app.core.auth.models import EmailVerificationStatus
from app.core.auth.models import Invitation
from app.core.auth.models import InvitationStatus
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.services.invitations import accept_invitation
from app.core.auth.services.invitations import reissue_platform_invitation
from app.core.auth.services.invitations import revoke_platform_invitation
from app.core.auth.services.passwords import verify_password
from app.core.auth.services.registration import register_user
from app.core.auth.services.registration import resend_verification
from app.core.auth.services.registration import verify_email
from app.core.auth.services.tokens import hash_token
from app.core.auth.services.users import create_user
from app.core.auth.services.users import get_user_by_email
from app.core.auth.services.users import soft_delete_user
from app.core.email.models import OutboundEmail
from app.core.exceptions import ForbiddenError
from app.core.exceptions import GoneError
from app.core.exceptions import NotFoundError
from app.core.platform_settings.service import update_platform_settings
from app.core.terms.service import ConsentRequiredError
from app.core.terms.service import StaleTermsVersionError
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


async def _count_outbound_emails(session: AsyncSession) -> int:
    return len((await session.execute(select(OutboundEmail))).scalars().all())


async def _count_pending_verifications(session: AsyncSession, user_id: UUID) -> int:
    result = await session.execute(
        EmailVerification.live_select()
        .where(col(EmailVerification.user_id) == user_id)
        .where(col(EmailVerification.status) == EmailVerificationStatus.PENDING),
    )
    return len(result.scalars().all())


def _verify_token(enqueue: MagicMock) -> str:
    """Recover the raw verification token from the enqueued task signature.

    The token rides `secret_context` (the task args), not the persisted
    `outbound_emails.context` — reading it here doubles as proof of that.
    """
    for call in enqueue.call_args_list:
        secret = call.kwargs["args"][1]
        if "verify_url" in secret:
            return secret["verify_url"].rsplit("token=", 1)[-1]
    raise AssertionError("no verification email was enqueued")


@pytest.mark.integration
async def test_register_user_missing_default_role_raises_runtime_error(
    db_session: AsyncSession,
) -> None:
    """Operator error if no active default role exists — surface as 500, not silently."""
    with pytest.raises(RuntimeError, match="No active default role"):
        await register_user(
            db_session,
            email="ada@example.com",
            password=SecretStr("supersecret-12345"),
            first_name=None,
            last_name=None,
        )


@pytest.mark.integration
async def test_register_user_creates_pending_account_with_hashed_password(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name="Ada",
        last_name="Lovelace",
    )

    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    assert user.status is UserStatus.PENDING
    assert user.email_verified_at is None
    assert user.password is not None
    assert verify_password("supersecret-12345", user.password)
    assert [r.name for r in user.roles] == ["red_teamer"]


@pytest.mark.integration
async def test_register_user_token_is_high_entropy_and_hashed(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )

    raw_token = _verify_token(enqueue_stub)
    verification = (await db_session.execute(EmailVerification.live_select())).scalars().one()

    assert len(raw_token) >= 32
    assert len(verification.token_hash) == 64
    assert verification.token_hash == hash_token(raw_token)
    assert verification.token_hash != raw_token


@pytest.mark.integration
async def test_register_user_sets_expires_at_from_settings(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    with time_machine.travel(now, tick=False):
        await register_user(
            db_session,
            email="ada@example.com",
            password=SecretStr("supersecret-12345"),
            first_name=None,
            last_name=None,
        )

    verification = (await db_session.execute(EmailVerification.live_select())).scalars().one()
    # Default email_verification_ttl_hours = 24
    assert verification.expires_at == now + timedelta(hours=24)


@pytest.mark.integration
async def test_register_user_expires_at_follows_platform_setting_override(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    await update_platform_settings(db_session, email_verification_ttl_hours=1)

    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    with time_machine.travel(now, tick=False):
        await register_user(
            db_session,
            email="ada@example.com",
            password=SecretStr("supersecret-12345"),
            first_name=None,
            last_name=None,
        )

    verification = (await db_session.execute(EmailVerification.live_select())).scalars().one()
    assert verification.expires_at == now + timedelta(hours=1)


@pytest.mark.integration
@pytest.mark.parametrize("conflict_status", [UserStatus.ACTIVE, UserStatus.INACTIVE])
async def test_register_user_existing_active_or_inactive_is_silent_no_op(
    db_session: AsyncSession,
    default_role: Role,
    conflict_status: UserStatus,
) -> None:
    await create_user(
        db_session,
        email="ada@example.com",
        status=conflict_status,
        roles=[default_role],
    )
    before_emails = await _count_outbound_emails(db_session)

    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )

    assert await _count_outbound_emails(db_session) == before_emails
    assert (await db_session.execute(EmailVerification.live_select())).scalars().all() == []


@pytest.mark.integration
async def test_register_user_pending_re_register_preserves_password(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    """Re-register against PENDING revokes the prior token but must not overwrite the password."""
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("first-pass-12345"),
        first_name=None,
        last_name=None,
    )
    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    original_hash = user.password
    assert original_hash is not None

    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("second-pass-67890"),
        first_name=None,
        last_name=None,
    )

    await db_session.refresh(user)
    assert user.password is not None
    assert user.password == original_hash
    assert verify_password("first-pass-12345", user.password)


@pytest.mark.integration
async def test_register_user_pending_re_register_revokes_old_and_issues_new(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )
    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    first = (await db_session.execute(EmailVerification.live_select())).scalars().one()
    first_hash = first.token_hash

    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )

    rows = (await db_session.execute(EmailVerification.live_select())).scalars().all()
    by_status = {row.status: row for row in rows}
    assert by_status[EmailVerificationStatus.REVOKED].token_hash == first_hash
    assert by_status[EmailVerificationStatus.REVOKED].revoked_at is not None
    assert by_status[EmailVerificationStatus.PENDING].token_hash != first_hash
    assert await _count_pending_verifications(db_session, user.id) == 1


@pytest.mark.integration
async def test_register_user_soft_deleted_account_creates_fresh_user(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    """Partial unique index lets a tombstoned email re-register from scratch."""
    original = await create_user(
        db_session,
        email="ada@example.com",
        status=UserStatus.ACTIVE,
        roles=[default_role],
    )
    await soft_delete_user(db_session, original, by_id=uuid4())

    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )

    rows = (await db_session.execute(User.live_select().where(col(User.email) == "ada@example.com"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id != original.id
    assert rows[0].status is UserStatus.PENDING


# --------------------------------------------------------------------------
# invited account self-onboarding (register re-issues the invitation;
# the email owner — not the registering caller — sets the password)
# --------------------------------------------------------------------------


async def _make_invitation(
    session: AsyncSession,
    user_id: UUID,
    *,
    status: InvitationStatus = InvitationStatus.PENDING,
    expires_at: datetime | None = None,
    object_type: ObjectType | None = None,
    object_id: UUID | None = None,
) -> Invitation:
    invitation = Invitation(
        user_id=user_id,
        token_hash=hash_token(f"invite-{user_id}-{status.value}"),
        expires_at=expires_at or (datetime.now(UTC) + timedelta(hours=48)),
        status=status,
        object_type=object_type,
        object_id=object_id,
    )
    session.add(invitation)
    await session.flush()
    await session.refresh(invitation)
    return invitation


async def _invited_user(session: AsyncSession, email: str = "grace@example.com") -> User:
    """An admin-provisioned, passwordless INVITED user with a non-default role."""
    invited_role = Role(name="reviewer", display_name="Reviewer", description="Reviewer", permissions=[])
    session.add(invited_role)
    await session.flush()
    return await create_user(session, email=email, status=UserStatus.INVITED, roles=[invited_role])


def _accept_token(enqueue: MagicMock) -> str:
    """Recover the raw invitation accept token from the enqueued task signature."""
    for call in enqueue.call_args_list:
        secret = call.kwargs["args"][1]
        if "accept_url" in secret:
            return secret["accept_url"].rsplit("token=", 1)[-1]
    raise AssertionError("no invitation email was enqueued")


async def _pending_platform_invitations(session: AsyncSession, user_id: UUID) -> list[Invitation]:
    result = await session.execute(
        Invitation.live_select()
        .where(col(Invitation.user_id) == user_id)
        .where(col(Invitation.status) == InvitationStatus.PENDING)
        .where(col(Invitation.object_type).is_(None)),
    )
    return list(result.scalars().all())


@pytest.mark.integration
async def test_register_user_invited_account_reissues_invitation_without_binding_credential(
    db_session: AsyncSession,
    enqueue_stub: MagicMock,
) -> None:
    """Registering against an INVITED account re-issues its invitation — it never sets a password.

    This is the security fix: an email-only request must not bind an
    attacker-chosen credential (or overwrite names) on a pre-existing account.
    """
    user = await _invited_user(db_session)
    prior = await _make_invitation(db_session, user.id)
    assert user.password is None

    await register_user(
        db_session,
        email="grace@example.com",
        password=SecretStr("attacker-pass-12345"),
        first_name="Mallory",
        last_name="Evil",
    )

    refreshed = await get_user_by_email(db_session, "grace@example.com")
    assert refreshed is not None
    # Account untouched apart from the re-issued invite: still passwordless, still INVITED.
    assert refreshed.status is UserStatus.INVITED
    assert refreshed.password is None
    assert refreshed.first_name is None
    assert refreshed.last_name is None
    assert [r.name for r in refreshed.roles] == ["reviewer"]
    # Onboarding goes via an invitation accept link, not an email-verification token.
    assert (await db_session.execute(EmailVerification.live_select())).scalars().all() == []
    assert _accept_token(enqueue_stub)
    # Prior pending invitation revoked, exactly one fresh pending one issued.
    await db_session.refresh(prior)
    assert prior.status is InvitationStatus.REVOKED
    pending = await _pending_platform_invitations(db_session, refreshed.id)
    assert len(pending) == 1
    assert pending[0].token_hash != prior.token_hash


@pytest.mark.integration
async def test_register_user_invited_reissue_then_accept_sets_owner_password(
    db_session: AsyncSession,
    enqueue_stub: MagicMock,
) -> None:
    """The re-issued link lets the email owner set the password and activate — the attacker's never lands."""
    user = await _invited_user(db_session)
    invitation = await _make_invitation(db_session, user.id)

    await register_user(
        db_session,
        email="grace@example.com",
        password=SecretStr("attacker-pass-12345"),
        first_name=None,
        last_name=None,
    )
    accept_token = _accept_token(enqueue_stub)

    await accept_invitation(
        db_session,
        raw_token=accept_token,
        password=SecretStr("owner-pass-67890"),
        first_name="Grace",
        last_name="Hopper",
    )

    refreshed = await get_user_by_email(db_session, "grace@example.com")
    assert refreshed is not None
    assert refreshed.status is UserStatus.ACTIVE
    assert refreshed.password is not None
    assert verify_password("owner-pass-67890", refreshed.password)
    assert not verify_password("attacker-pass-12345", refreshed.password)
    assert refreshed.first_name == "Grace"
    # Invite-time roles are preserved.
    assert [r.name for r in refreshed.roles] == ["reviewer"]
    # The original invitation was revoked by the re-issue; the accepted one is the fresh token.
    await db_session.refresh(invitation)
    assert invitation.status is InvitationStatus.REVOKED


@pytest.mark.integration
async def test_register_user_invited_with_no_invitation_row_still_reissues(
    db_session: AsyncSession,
    enqueue_stub: MagicMock,
) -> None:
    """An invited account whose invitation was pruned still gets a fresh accept link."""
    user = await _invited_user(db_session)

    await register_user(
        db_session,
        email="grace@example.com",
        password=SecretStr("first-pass-12345"),
        first_name=None,
        last_name=None,
    )

    assert _accept_token(enqueue_stub)
    pending = await _pending_platform_invitations(db_session, user.id)
    assert len(pending) == 1
    await db_session.refresh(user)
    assert user.status is UserStatus.INVITED
    assert user.password is None


# --------------------------------------------------------------------------
# verify_email
# --------------------------------------------------------------------------


async def _register_and_extract_token(
    session: AsyncSession, enqueue: MagicMock, *, first_name: str | None = "Ada"
) -> str:
    await register_user(
        session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=first_name,
        last_name=None,
    )
    return _verify_token(enqueue)


@pytest.mark.integration
async def test_verify_email_activates_user_and_marks_verification(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    token = await _register_and_extract_token(db_session, enqueue_stub)

    user = await verify_email(db_session, raw_token=token)

    assert user.status is UserStatus.ACTIVE
    assert user.email_verified_at is not None

    verification = (await db_session.execute(EmailVerification.live_select())).scalars().one()
    assert verification.status is EmailVerificationStatus.VERIFIED
    assert verification.verified_at is not None


@pytest.mark.integration
async def test_verify_email_sends_account_activated_with_first_name(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    token = await _register_and_extract_token(db_session, enqueue_stub, first_name="Ada")

    await verify_email(db_session, raw_token=token)

    emails = (await db_session.execute(select(OutboundEmail))).scalars().all()
    activated = next(e for e in emails if e.template_name == "account_activated")
    assert activated.context["user_name"] == "Ada"


@pytest.mark.integration
async def test_verify_email_falls_back_to_email_when_first_name_missing(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    token = await _register_and_extract_token(db_session, enqueue_stub, first_name=None)

    await verify_email(db_session, raw_token=token)

    emails = (await db_session.execute(select(OutboundEmail))).scalars().all()
    activated = next(e for e in emails if e.template_name == "account_activated")
    assert activated.context["user_name"] == "ada@example.com"


@pytest.mark.integration
async def test_verify_email_unknown_token_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await verify_email(db_session, raw_token="no-such-token")


@pytest.mark.integration
async def test_verify_email_second_call_raises_gone(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    token = await _register_and_extract_token(db_session, enqueue_stub)
    await verify_email(db_session, raw_token=token)

    with pytest.raises(GoneError, match="verified"):
        await verify_email(db_session, raw_token=token)


@pytest.mark.integration
async def test_verify_email_overdue_pending_surfaces_as_expired(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    token = await _register_and_extract_token(db_session, enqueue_stub)
    row = (await db_session.execute(EmailVerification.live_select())).scalars().one()
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.add(row)
    await db_session.flush()

    with pytest.raises(GoneError, match="expired"):
        await verify_email(db_session, raw_token=token)

    await db_session.refresh(row)
    # Lazy expiry is read-only.
    assert row.status is EmailVerificationStatus.PENDING


@pytest.mark.integration
async def test_verify_email_soft_deleted_user_raises_gone(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    token = await _register_and_extract_token(db_session, enqueue_stub)
    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    await soft_delete_user(db_session, user, by_id=uuid4())

    with pytest.raises(GoneError):
        await verify_email(db_session, raw_token=token)


@pytest.mark.integration
async def test_verify_email_survives_mail_side_flush_failure(
    db_session: AsyncSession,
    default_role: Role,
    monkeypatch: pytest.MonkeyPatch,
    enqueue_stub: MagicMock,
) -> None:
    """Savepoint regression: a DB error in the account-activated mail must not undo activation.

    The activation is flushed *before* the `begin_nested()` block, so a failing
    `send_email` rolls back only the savepoint. Without it, the poisoned session
    would make the outer `@transactional` commit raise and bounce the user back
    to PENDING. We stand in for that commit with an explicit `commit()` and
    assert the ACTIVE user + VERIFIED row persist.
    """
    token = await _register_and_extract_token(db_session, enqueue_stub)

    async def _send_email_failing_flush(session: AsyncSession, *_args: object, **_kwargs: object) -> None:
        # Reproduce the documented failure mode: an IntegrityError during the
        # audit-row flush. A bogus FK puts the session into pending-rollback,
        # exactly as a real OutboundEmail insert failure would.
        session.add(
            EmailVerification(
                user_id=uuid4(),  # no such user → FK violation on flush
                token_hash="poison-token-hash",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        )
        await session.flush()

    monkeypatch.setattr(email_module, "send_email", _send_email_failing_flush)

    user = await verify_email(db_session, raw_token=token)
    await db_session.commit()

    assert user.status is UserStatus.ACTIVE

    persisted = await get_user_by_email(db_session, "ada@example.com")
    assert persisted is not None
    assert persisted.status is UserStatus.ACTIVE
    assert persisted.email_verified_at is not None

    verification = (await db_session.execute(EmailVerification.live_select())).scalars().one()
    assert verification.status is EmailVerificationStatus.VERIFIED
    assert verification.verified_at is not None


@pytest.mark.integration
async def test_verify_email_accepts_group_invitation_for_self_signup(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    """A self-signup group-invited while still PENDING has that invitation consumed on verify.

    `invite_to_group` issues a group-scoped invitation to a still-onboarding
    account; activating via the email-verification token must not leave it
    lingering PENDING against the now-active account.
    """
    token = await _register_and_extract_token(db_session, enqueue_stub)
    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    invitation = await _make_invitation(
        db_session,
        user.id,
        object_type=ObjectType.EVALUATION_GROUP,
        object_id=uuid4(),
    )

    await verify_email(db_session, raw_token=token)

    await db_session.refresh(invitation)
    assert invitation.status is InvitationStatus.ACCEPTED
    assert invitation.accepted_at is not None


@pytest.mark.integration
async def test_register_user_does_not_resurrect_a_revoked_invitation(
    db_session: AsyncSession,
    enqueue_stub: MagicMock,
) -> None:
    """An admin-revoked invitation stays dead against an anonymous re-registration.

    Otherwise `/auth/register` hands back the very accept link the revoke killed —
    against an account that still carries its invite-time roles.
    """
    user = await _invited_user(db_session)
    await _make_invitation(db_session, user.id)
    await revoke_platform_invitation(db_session, user)
    enqueue_stub.reset_mock()

    await register_user(
        db_session,
        email="grace@example.com",
        password=SecretStr("attacker-pass-12345"),
        first_name="Mallory",
        last_name="Evil",
    )

    assert await _pending_platform_invitations(db_session, user.id) == []
    assert (await db_session.execute(EmailVerification.live_select())).scalars().all() == []
    assert enqueue_stub.call_count == 0
    refreshed = await get_user_by_email(db_session, "grace@example.com")
    assert refreshed is not None
    assert refreshed.password is None


@pytest.mark.integration
async def test_register_user_reissues_again_after_a_revoke_is_followed_by_a_fresh_invite(
    db_session: AsyncSession,
    enqueue_stub: MagicMock,
) -> None:
    """Revoking is not a permanent ban — a fresh admin invite re-opens self-service onboarding."""
    user = await _invited_user(db_session)
    await _make_invitation(db_session, user.id)
    await revoke_platform_invitation(db_session, user)
    await reissue_platform_invitation(db_session, user)
    enqueue_stub.reset_mock()

    await register_user(
        db_session,
        email="grace@example.com",
        password=SecretStr("whatever-pass-12345"),
        first_name=None,
        last_name=None,
    )

    assert len(await _pending_platform_invitations(db_session, user.id)) == 1
    assert enqueue_stub.call_count == 1


@pytest.mark.integration
async def test_register_user_invite_only_refuses_uniformly_before_any_write(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    """The refusal must not depend on the email — same error for a fresh, taken, and invited address.

    `INVITED` matters most: it is the one arm with a side effect (invitation re-issue + mail),
    so the refusal must also prove no new invitation row and no mail went out.
    """
    await create_user(db_session, email="taken@example.com", status=UserStatus.ACTIVE, roles=[default_role])
    invited = await create_user(
        db_session, email="invited@example.com", status=UserStatus.INVITED, roles=[default_role]
    )
    invitation = await _make_invitation(db_session, invited.id)
    await update_platform_settings(db_session, invite_only=True)

    for email in ("fresh@example.com", "taken@example.com", "invited@example.com"):
        with pytest.raises(ForbiddenError):
            await register_user(
                db_session,
                email=email,
                password=SecretStr("supersecret-12345"),
                first_name=None,
                last_name=None,
            )

    assert await get_user_by_email(db_session, "fresh@example.com") is None
    # The surviving pending row is the *original* — a re-issue would have revoked it and
    # minted a fresh one (same count), so the id is what proves the arm never ran.
    assert [inv.id for inv in await _pending_platform_invitations(db_session, invited.id)] == [invitation.id]
    assert await _count_outbound_emails(db_session) == 0


@pytest.mark.integration
async def test_verify_email_stays_open_under_invite_only(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    """A token issued while signup was open must survive the toggle — it governs new signups only."""
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )
    raw_token = _verify_token(enqueue_stub)
    await update_platform_settings(db_session, invite_only=True)

    user = await verify_email(db_session, raw_token=raw_token)

    assert user.status is UserStatus.ACTIVE


@pytest.mark.integration
async def test_resend_verification_reissues_for_pending_and_revokes_old(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )
    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    enqueue_stub.reset_mock()

    await resend_verification(db_session, email="ada@example.com")

    rows = (await db_session.execute(EmailVerification.live_select())).scalars().all()
    by_status = {row.status for row in rows}
    assert len(rows) == 2
    assert by_status == {EmailVerificationStatus.PENDING, EmailVerificationStatus.REVOKED}
    assert await _count_pending_verifications(db_session, user.id) == 1
    assert enqueue_stub.call_count == 1  # exactly one fresh verification mail


@pytest.mark.integration
async def test_resend_verification_is_a_silent_noop_for_non_pending(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    """Unknown, ACTIVE, INACTIVE, and INVITED emails all end the same way: no rows, no mail.

    For INVITED that also means no invitation re-issue — that recovery path
    belongs to /auth/register, and duplicating it here would mint tokens for an
    account that must onboard via accept_invitation.
    """
    await create_user(db_session, email="active@example.com", status=UserStatus.ACTIVE, roles=[default_role])
    await create_user(db_session, email="inactive@example.com", status=UserStatus.INACTIVE, roles=[default_role])
    invited = await _invited_user(db_session, email="invited@example.com")
    enqueue_stub.reset_mock()

    for email in ("ghost@example.com", "active@example.com", "inactive@example.com", "invited@example.com"):
        await resend_verification(db_session, email=email)

    assert (await db_session.execute(EmailVerification.live_select())).scalars().all() == []
    assert await _pending_platform_invitations(db_session, invited.id) == []
    assert enqueue_stub.call_count == 0


@pytest.mark.integration
async def test_resend_verification_expires_at_follows_platform_setting_override(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    """The resend path reads the admin knob too — not the raw env default."""
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )
    await update_platform_settings(db_session, email_verification_ttl_hours=3)

    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    with time_machine.travel(now, tick=False):
        await resend_verification(db_session, email="ada@example.com")

    fresh = (
        (
            await db_session.execute(
                EmailVerification.live_select().where(col(EmailVerification.status) == EmailVerificationStatus.PENDING)
            )
        )
        .scalars()
        .one()
    )
    assert fresh.expires_at == now + timedelta(hours=3)


@pytest.mark.integration
async def test_resend_verification_stays_open_under_invite_only(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    """A signup that predates the invite-only flip may still refresh its link."""
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )
    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    await update_platform_settings(db_session, invite_only=True)
    enqueue_stub.reset_mock()

    await resend_verification(db_session, email="ada@example.com")

    assert await _count_pending_verifications(db_session, user.id) == 1
    assert enqueue_stub.call_count == 1


@pytest.mark.integration
async def test_resend_verification_no_ops_when_the_account_activates_before_the_lock(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
) -> None:
    """A verify landing between the unlocked lookup and the lock must not mint a second token.

    The out-of-band `UPDATE` uses ``synchronize_session=False``, so the unlocked
    `get_user_by_email` still hands back a PENDING instance from the identity map and
    only the locked refetch sees ACTIVE — which is the whole point of the re-check, and
    what silently failed before `get_user` set `populate_existing`.
    """
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )
    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    await db_session.execute(
        update(User)
        .where(col(User.id) == user.id)
        .values(status=UserStatus.ACTIVE)
        .execution_options(synchronize_session=False)
    )
    enqueue_stub.reset_mock()

    await resend_verification(db_session, email="ada@example.com")

    rows = (await db_session.execute(EmailVerification.live_select())).scalars().all()
    assert [row.status for row in rows] == [EmailVerificationStatus.PENDING]  # untouched, not revoked+reissued
    assert enqueue_stub.call_count == 0


@pytest.mark.integration
async def test_resend_verification_swallows_a_user_vanishing_before_the_lock(
    db_session: AsyncSession,
    default_role: Role,
    enqueue_stub: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Soft-deleted between the unlocked lookup and the lock — a 404 would leak the email existed."""
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )
    enqueue_stub.reset_mock()

    async def vanish(*_args: object, **_kwargs: object) -> User:
        raise NotFoundError("user vanished mid-flight")

    monkeypatch.setattr("app.core.auth.services.registration.get_user", vanish)

    await resend_verification(db_session, email="ada@example.com")  # must not raise

    assert enqueue_stub.call_count == 0


@pytest.mark.integration
async def test_register_user_records_consent_against_the_current_version(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    document = await publish_terms(db_session, version="1.0", content="Terms")

    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
        consent_terms=True,
        consent_emails=True,
        terms_id=document.id,
    )

    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    assert user.accepted_terms_id == document.id
    assert user.terms_accepted_at is not None
    assert user.consent_emails is True


@pytest.mark.integration
async def test_register_user_refuses_a_declined_consent_once_published(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    await publish_terms(db_session, version="1.0", content="Terms")

    with pytest.raises(ConsentRequiredError):
        await register_user(
            db_session,
            email="ada@example.com",
            password=SecretStr("supersecret-12345"),
            first_name=None,
            last_name=None,
            consent_terms=False,
        )

    assert await get_user_by_email(db_session, "ada@example.com") is None


@pytest.mark.integration
async def test_register_user_refuses_before_touching_the_email(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    """The consent guard must be enumeration-safe: same refusal whether the email exists."""
    await publish_terms(db_session, version="1.0", content="Terms")
    await create_user(db_session, email="taken@example.com", status=UserStatus.ACTIVE, roles=[default_role])

    for email in ("taken@example.com", "fresh@example.com"):
        with pytest.raises(ConsentRequiredError):
            await register_user(
                db_session,
                email=email,
                password=SecretStr("supersecret-12345"),
                first_name=None,
                last_name=None,
                consent_terms=False,
            )


@pytest.mark.integration
async def test_register_user_pending_re_register_preserves_consent(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    """Re-registering must not rewrite consent, for the same reason it leaves the password alone."""
    document = await publish_terms(db_session, version="1.0", content="Terms")
    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("first-pass-12345"),
        first_name=None,
        last_name=None,
        consent_terms=True,
        consent_emails=True,
        terms_id=document.id,
    )
    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    accepted_at = user.terms_accepted_at

    await register_user(
        db_session,
        email="ada@example.com",
        password=SecretStr("second-pass-67890"),
        first_name=None,
        last_name=None,
        consent_terms=True,
        consent_emails=False,
        terms_id=document.id,
    )

    await db_session.refresh(user)
    assert user.terms_accepted_at == accepted_at
    assert user.consent_emails is True


@pytest.mark.integration
async def test_accept_invitation_records_consent(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    document = await publish_terms(db_session, version="1.0", content="Terms")
    user = await create_user(db_session, email="invitee@example.com", roles=[default_role])
    raw_token = "invite-token-for-consent"
    db_session.add(
        Invitation(
            user_id=user.id,
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(hours=48),
        )
    )
    await db_session.flush()

    accepted = await accept_invitation(
        db_session,
        raw_token=raw_token,
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
        consent_terms=True,
        consent_emails=True,
        terms_id=document.id,
    )

    assert accepted.accepted_terms_id == document.id
    assert accepted.consent_emails is True


@pytest.mark.integration
async def test_accept_invitation_refuses_a_declined_consent(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    await publish_terms(db_session, version="1.0", content="Terms")
    user = await create_user(db_session, email="invitee@example.com", roles=[default_role])
    raw_token = "invite-token-declining-consent"
    db_session.add(
        Invitation(
            user_id=user.id,
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(hours=48),
        )
    )
    await db_session.flush()

    with pytest.raises(ConsentRequiredError):
        await accept_invitation(
            db_session,
            raw_token=raw_token,
            password=SecretStr("supersecret-12345"),
            first_name=None,
            last_name=None,
            consent_terms=False,
        )

    await db_session.refresh(user)
    assert user.status is UserStatus.INVITED
    assert user.password is None


@pytest.mark.integration
async def test_register_user_refuses_consent_for_a_superseded_version(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    """A publish between form render and submit must not be recorded as read."""
    with time_machine.travel(datetime(2026, 1, 1, tzinfo=UTC), tick=False):
        stale = await publish_terms(db_session, version="1.0", content="First")
    with time_machine.travel(datetime(2026, 6, 1, tzinfo=UTC), tick=False):
        await publish_terms(db_session, version="2.0", content="Second")

    with pytest.raises(StaleTermsVersionError):
        await register_user(
            db_session,
            email="ada@example.com",
            password=SecretStr("supersecret-12345"),
            first_name=None,
            last_name=None,
            consent_terms=True,
            terms_id=stale.id,
        )

    assert await get_user_by_email(db_session, "ada@example.com") is None


@pytest.mark.integration
async def test_accept_invitation_refuses_consent_for_a_superseded_version(
    db_session: AsyncSession,
    default_role: Role,
) -> None:
    with time_machine.travel(datetime(2026, 1, 1, tzinfo=UTC), tick=False):
        stale = await publish_terms(db_session, version="1.0", content="First")
    with time_machine.travel(datetime(2026, 6, 1, tzinfo=UTC), tick=False):
        await publish_terms(db_session, version="2.0", content="Second")
    user = await create_user(db_session, email="invitee@example.com", roles=[default_role])
    raw_token = "invite-token-stale-terms"
    db_session.add(
        Invitation(
            user_id=user.id,
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(hours=48),
        )
    )
    await db_session.flush()

    with pytest.raises(StaleTermsVersionError):
        await accept_invitation(
            db_session,
            raw_token=raw_token,
            password=SecretStr("supersecret-12345"),
            first_name=None,
            last_name=None,
            consent_terms=True,
            terms_id=stale.id,
        )

    await db_session.refresh(user)
    assert user.password is None
