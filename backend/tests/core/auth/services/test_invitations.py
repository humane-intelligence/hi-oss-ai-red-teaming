"""Integration tests for `app.core.auth.services.invitations`."""

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
from app.core.auth.models import Invitation
from app.core.auth.models import InvitationStatus
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.roles import ROLE_PERMISSIONS
from app.core.auth.roles import SystemRole
from app.core.auth.services.invitations import InvitationPreview
from app.core.auth.services.invitations import InvitationWithToken
from app.core.auth.services.invitations import accept_invitation
from app.core.auth.services.invitations import get_invitation_preview
from app.core.auth.services.invitations import invite_to_platform
from app.core.auth.services.invitations import reissue_platform_invitation
from app.core.auth.services.passwords import verify_password
from app.core.auth.services.tokens import hash_token
from app.core.auth.services.users import create_user
from app.core.auth.services.users import soft_delete_user
from app.core.email.models import OutboundEmail
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import ForbiddenError
from app.core.exceptions import GoneError
from app.core.exceptions import NotFoundError
from tests.conftest import session_user_from


@pytest.fixture(autouse=True)
def _celery_enqueue_stub(celery_enqueue_stub: MagicMock) -> None:
    """Every invitation path here dispatches mail, so the shared spy is module-wide."""


@pytest_asyncio.fixture
async def annotator_role(db_session: AsyncSession) -> Role:
    role = Role(name="annotator", permissions=["evaluations:annotate"])
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def admin_role(db_session: AsyncSession) -> Role:
    role = Role(name="admin", permissions=sorted(ROLE_PERMISSIONS[SystemRole.ADMIN]))
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def owner_role(db_session: AsyncSession) -> Role:
    role = Role(name="owner", permissions=sorted(ROLE_PERMISSIONS[SystemRole.OWNER]))
    db_session.add(role)
    await db_session.flush()
    await db_session.refresh(role)
    return role


@pytest_asyncio.fixture
async def inviter(db_session: AsyncSession, admin_role: Role) -> User:
    return await create_user(
        db_session,
        email="admin@example.com",
        first_name="Bob",
        last_name="Smith",
        email_verified=True,
        status=UserStatus.ACTIVE,
        roles=[admin_role],
    )


async def _count_pending_invitations(session: AsyncSession, user_id: UUID) -> int:
    result = await session.execute(
        Invitation.live_select()
        .where(col(Invitation.user_id) == user_id)
        .where(col(Invitation.status) == InvitationStatus.PENDING),
    )
    return len(result.scalars().all())


async def _count_outbound_emails(session: AsyncSession) -> int:
    result = await session.execute(select(OutboundEmail))
    return len(result.scalars().all())


@pytest.mark.integration
async def test_invite_creates_user_and_invitation_for_new_email(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    assert isinstance(result, InvitationWithToken)
    assert result.raw_token  # non-empty
    assert result.invitation.token_hash == hash_token(result.raw_token)
    assert result.invitation.status is InvitationStatus.PENDING
    assert result.invitation.invited_by_user_id == inviter.id

    user_row = (await db_session.execute(User.live_select().where(col(User.email) == "ada@example.com"))).scalar_one()
    assert user_row.status is UserStatus.INVITED
    assert user_row.password is None


@pytest.mark.integration
async def test_invite_sends_email_by_default(db_session: AsyncSession, inviter: User, annotator_role: Role) -> None:
    before = await _count_outbound_emails(db_session)

    await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    after = await _count_outbound_emails(db_session)
    assert after == before + 1


@pytest.mark.integration
async def test_invite_skips_email_when_side_effects_disabled(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    """Dry-run flips `send_side_effects=False` — no OutboundEmail row should land."""
    before = await _count_outbound_emails(db_session)

    await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
        send_side_effects=False,
    )

    after = await _count_outbound_emails(db_session)
    assert after == before


@pytest.mark.integration
async def test_invite_unknown_role_raises_bad_request(db_session: AsyncSession, inviter: User) -> None:
    with pytest.raises(BadRequestError, match="Unknown role"):
        await invite_to_platform(
            db_session,
            email="ada@example.com",
            role_ids=[uuid4()],
            inviter=session_user_from(inviter),
        )


@pytest.mark.integration
async def test_invite_role_caller_cannot_grant_raises_forbidden(
    db_session: AsyncSession, owner_role: Role, admin_role: Role
) -> None:
    """An owner may invite many roles but not admin — `users:manage_admin` is withheld."""
    owner = await create_user(
        db_session,
        email="owner@example.com",
        email_verified=True,
        status=UserStatus.ACTIVE,
        roles=[owner_role],
    )

    with pytest.raises(ForbiddenError, match=r"cannot assign role\(s\): admin"):
        await invite_to_platform(
            db_session,
            email="ada@example.com",
            role_ids=[admin_role.id],
            inviter=session_user_from(owner),
        )


@pytest.mark.integration
async def test_invite_grantable_role_succeeds_for_owner(
    db_session: AsyncSession, owner_role: Role, annotator_role: Role
) -> None:
    """An owner holds `users:invite` and annotator isn't elevated, so the invite goes through."""
    owner = await create_user(
        db_session,
        email="owner@example.com",
        email_verified=True,
        status=UserStatus.ACTIVE,
        roles=[owner_role],
    )

    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(owner),
        send_side_effects=False,
    )

    assert result.invitation.status is InvitationStatus.PENDING


@pytest.mark.integration
async def test_invite_active_user_raises_conflict(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    await create_user(
        db_session,
        email="ada@example.com",
        status=UserStatus.ACTIVE,
        roles=[annotator_role],
    )

    with pytest.raises(ConflictError, match="already active"):
        await invite_to_platform(
            db_session,
            email="ada@example.com",
            role_ids=[annotator_role.id],
            inviter=session_user_from(inviter),
        )


@pytest.mark.integration
async def test_invite_pending_user_raises_conflict(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    await create_user(
        db_session,
        email="ada@example.com",
        status=UserStatus.PENDING,
        roles=[annotator_role],
    )

    with pytest.raises(ConflictError, match="registration in progress"):
        await invite_to_platform(
            db_session,
            email="ada@example.com",
            role_ids=[annotator_role.id],
            inviter=session_user_from(inviter),
        )


@pytest.mark.integration
async def test_invite_inactive_user_raises_conflict(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    await create_user(
        db_session,
        email="ada@example.com",
        status=UserStatus.INACTIVE,
        roles=[annotator_role],
    )

    with pytest.raises(ConflictError, match="deactivated"):
        await invite_to_platform(
            db_session,
            email="ada@example.com",
            role_ids=[annotator_role.id],
            inviter=session_user_from(inviter),
        )


@pytest.mark.integration
async def test_invite_invited_user_same_role_revokes_old_and_issues_new(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    first = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )
    user_id = first.invitation.user_id

    second = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    await db_session.refresh(first.invitation)
    assert first.invitation.status is InvitationStatus.REVOKED
    assert first.invitation.revoked_at is not None
    assert second.invitation.status is InvitationStatus.PENDING
    assert second.invitation.token_hash != first.invitation.token_hash
    pending = await _count_pending_invitations(db_session, user_id)
    assert pending == 1


@pytest.mark.integration
async def test_invite_invited_user_different_role_raises_conflict(
    db_session: AsyncSession, inviter: User, annotator_role: Role, admin_role: Role
) -> None:
    await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    with pytest.raises(ConflictError, match="different role assignment"):
        await invite_to_platform(
            db_session,
            email="ada@example.com",
            role_ids=[admin_role.id],
            inviter=session_user_from(inviter),
        )


@pytest.mark.integration
async def test_invite_soft_deleted_user_creates_fresh_user(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    """Partial unique index on email lets a tombstoned account be re-invited."""
    original = await create_user(
        db_session,
        email="ada@example.com",
        status=UserStatus.ACTIVE,
        roles=[annotator_role],
    )
    await soft_delete_user(db_session, original, by_id=uuid4())

    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    assert result.invitation.user_id != original.id


@pytest.mark.integration
async def test_invite_token_is_high_entropy_and_hashed(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    # secrets.token_urlsafe(32) yields ~43 url-safe characters
    assert len(result.raw_token) >= 32
    # The hex digest is 64 chars and never equal to the raw token
    assert len(result.invitation.token_hash) == 64
    assert result.invitation.token_hash != result.raw_token


@pytest.mark.integration
async def test_invite_sets_expires_at_from_settings(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    now = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    with time_machine.travel(now, tick=False):
        result = await invite_to_platform(
            db_session,
            email="ada@example.com",
            role_ids=[annotator_role.id],
            inviter=session_user_from(inviter),
        )

    # Default invitation_ttl_hours = 168 (7 days).
    assert result.invitation.expires_at == now + timedelta(hours=168)


# --------------------------------------------------------------------------
# Accept flow — get_invitation_preview + accept_invitation
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_preview_returns_email_roles_and_inviter_name(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    preview = await get_invitation_preview(db_session, result.raw_token)

    assert isinstance(preview, InvitationPreview)
    assert preview.email == "ada@example.com"
    assert preview.role_names == ["annotator"]
    assert preview.inviter_name == "Bob Smith"
    assert preview.expires_at == result.invitation.expires_at


@pytest.mark.integration
async def test_preview_falls_back_to_inviter_email_when_name_missing(
    db_session: AsyncSession, annotator_role: Role, admin_role: Role
) -> None:
    inviter_no_name = await create_user(
        db_session,
        email="nameless@example.com",
        email_verified=True,
        status=UserStatus.ACTIVE,
        roles=[admin_role],
    )
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter_no_name),
    )

    preview = await get_invitation_preview(db_session, result.raw_token)

    assert preview.inviter_name == "nameless@example.com"


@pytest.mark.integration
async def test_preview_unknown_token_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await get_invitation_preview(db_session, "no-such-token")


@pytest.mark.integration
async def test_preview_revoked_invitation_raises_gone(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    first = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )
    # Re-invite revokes the first token.
    await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    with pytest.raises(GoneError, match="revoked"):
        await get_invitation_preview(db_session, first.raw_token)


@pytest.mark.integration
async def test_preview_overdue_pending_surfaces_as_expired(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    """Effective status reads EXPIRED for an overdue PENDING row; stored status is unchanged."""
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )
    result.invitation.expires_at = datetime.now(UTC) - timedelta(hours=1)
    db_session.add(result.invitation)
    await db_session.flush()

    with pytest.raises(GoneError, match="expired"):
        await get_invitation_preview(db_session, result.raw_token)

    await db_session.refresh(result.invitation)
    # Stored status is unchanged — lazy expiry is read-only.
    assert result.invitation.status is InvitationStatus.PENDING


@pytest.mark.integration
async def test_accept_activates_user_and_marks_invitation_accepted(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    user = await accept_invitation(
        db_session,
        raw_token=result.raw_token,
        password=SecretStr("supersecret-12345"),
        first_name="Ada",
        last_name="Lovelace",
    )

    assert user.status is UserStatus.ACTIVE
    assert user.email_verified_at is not None
    assert user.first_name == "Ada"
    assert user.last_name == "Lovelace"
    assert user.password is not None
    assert verify_password("supersecret-12345", user.password)

    await db_session.refresh(result.invitation)
    assert result.invitation.status is InvitationStatus.ACCEPTED
    assert result.invitation.accepted_at is not None


@pytest.mark.integration
async def test_accept_rereads_the_invitation_after_taking_the_locks(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    """A revoke landing between the token lookup and the locks must still be seen.

    The lookup is unlocked (it only resolves which account to lock), so the row it
    returns can be stale by the time the user lock is held. Simulated by updating
    the row underneath the identity-mapped object: a plain `execute` hands back the
    cached instance, so only the post-lock re-read can notice.
    """
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )
    await db_session.execute(
        update(Invitation).where(col(Invitation.id) == result.invitation.id).values(status=InvitationStatus.REVOKED),
    )

    with pytest.raises(GoneError, match="revoked"):
        await accept_invitation(
            db_session,
            raw_token=result.raw_token,
            password=SecretStr("supersecret-12345"),
            first_name="Ada",
            last_name="Lovelace",
        )


@pytest.mark.integration
async def test_accept_sends_account_activated_with_first_name(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    await accept_invitation(
        db_session,
        raw_token=result.raw_token,
        password=SecretStr("supersecret-12345"),
        first_name="Ada",
        last_name="Lovelace",
    )

    emails = (await db_session.execute(select(OutboundEmail))).scalars().all()
    activated = next(e for e in emails if e.template_name == "account_activated")
    assert activated.recipient == "ada@example.com"
    assert activated.context["user_name"] == "Ada"


@pytest.mark.integration
async def test_accept_account_activated_falls_back_to_email_when_first_name_missing(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    await accept_invitation(
        db_session,
        raw_token=result.raw_token,
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )

    emails = (await db_session.execute(select(OutboundEmail))).scalars().all()
    activated = next(e for e in emails if e.template_name == "account_activated")
    assert activated.context["user_name"] == "ada@example.com"


@pytest.mark.integration
async def test_accept_survives_mail_side_flush_failure(
    db_session: AsyncSession,
    inviter: User,
    annotator_role: Role,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Savepoint regression: a DB error in the account-activated mail must not undo acceptance.

    The activation is flushed *before* the `begin_nested()` block, so a failing
    `send_email` rolls back only the savepoint. Without it, the poisoned session
    would make the outer `@transactional` commit raise and bounce the user back
    to INVITED. We stand in for that commit with an explicit `commit()` and
    assert the ACTIVE user + ACCEPTED invitation persist.
    """
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    async def _send_email_failing_flush(session: AsyncSession, *_args: object, **_kwargs: object) -> None:
        # Reproduce the documented failure mode: a bogus FK puts the session
        # into pending-rollback on flush, exactly as a real OutboundEmail
        # insert failure would.
        session.add(
            Invitation(
                user_id=uuid4(),  # no such user → FK violation on flush
                invited_by_user_id=None,
                token_hash="poison-token-hash",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            ),
        )
        await session.flush()

    monkeypatch.setattr(email_module, "send_email", _send_email_failing_flush)

    user = await accept_invitation(
        db_session,
        raw_token=result.raw_token,
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )
    await db_session.commit()

    assert user.status is UserStatus.ACTIVE

    await db_session.refresh(result.invitation)
    assert result.invitation.status is InvitationStatus.ACCEPTED


@pytest.mark.integration
async def test_accept_sweeps_sibling_pending_invitation(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    """Accepting one invitation completes a parallel live invitation in another scope too.

    Onboarding happens once; a sibling (here a group-scoped invite) must not
    linger PENDING against the now-active account.
    """
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )
    sibling = Invitation(
        user_id=result.user.id,
        invited_by_user_id=inviter.id,
        token_hash=hash_token("sibling-token"),
        expires_at=datetime.now(UTC) + timedelta(hours=48),
        object_type=ObjectType.EVALUATION_GROUP,
        object_id=uuid4(),
    )
    db_session.add(sibling)
    await db_session.flush()

    await accept_invitation(
        db_session,
        raw_token=result.raw_token,
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )

    await db_session.refresh(sibling)
    assert sibling.status is InvitationStatus.ACCEPTED
    assert sibling.accepted_at is not None


@pytest.mark.integration
async def test_accept_leaves_expired_sibling_invitation_untouched(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    """A lazily-expired sibling was never valid to accept — the sweep skips it (stays PENDING)."""
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )
    sibling = Invitation(
        user_id=result.user.id,
        invited_by_user_id=inviter.id,
        token_hash=hash_token("sibling-token"),
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        object_type=ObjectType.EVALUATION_GROUP,
        object_id=uuid4(),
    )
    db_session.add(sibling)
    await db_session.flush()

    await accept_invitation(
        db_session,
        raw_token=result.raw_token,
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )

    await db_session.refresh(sibling)
    assert sibling.status is InvitationStatus.PENDING
    assert sibling.accepted_at is None


@pytest.mark.integration
async def test_accept_is_idempotent_second_call_raises_gone(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    await accept_invitation(
        db_session,
        raw_token=result.raw_token,
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )

    with pytest.raises(GoneError, match="accepted"):
        await accept_invitation(
            db_session,
            raw_token=result.raw_token,
            password=SecretStr("anothersecret-67890"),
            first_name=None,
            last_name=None,
        )


@pytest.mark.integration
async def test_accept_rejects_deactivated_account(db_session: AsyncSession) -> None:
    """A live token whose account is INACTIVE must not reactivate it / set a password.

    Guards the whitelist in `accept_invitation`: only INVITED / PENDING accounts
    onboard. Without it a stale token is an unauthenticated reactivation + reset.
    """
    user = User(email="ada@example.com", status=UserStatus.INACTIVE)
    db_session.add(user)
    await db_session.flush()
    raw_token = "deactivated-account-token"
    db_session.add(
        Invitation(
            user_id=user.id,
            invited_by_user_id=None,
            token_hash=hash_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    await db_session.flush()

    with pytest.raises(GoneError, match="awaiting onboarding"):
        await accept_invitation(
            db_session,
            raw_token=raw_token,
            password=SecretStr("supersecret-12345"),
            first_name=None,
            last_name=None,
        )

    await db_session.refresh(user)
    assert user.status is UserStatus.INACTIVE
    assert user.password is None


@pytest.mark.integration
async def test_accept_unknown_token_raises_not_found(db_session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await accept_invitation(
            db_session,
            raw_token="no-such-token",
            password=SecretStr("supersecret-12345"),
            first_name=None,
            last_name=None,
        )


@pytest.mark.integration
async def test_accept_omitted_names_leaves_fields_unchanged(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    result = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    user = await accept_invitation(
        db_session,
        raw_token=result.raw_token,
        password=SecretStr("supersecret-12345"),
        first_name=None,
        last_name=None,
    )

    assert user.first_name is None
    assert user.last_name is None
    assert user.status is UserStatus.ACTIVE


@pytest.mark.integration
async def test_admin_resend_attributes_the_inviter_and_supersedes_the_prior_token(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    first = await invite_to_platform(
        db_session,
        email="pending@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    resent = await reissue_platform_invitation(db_session, first.user, inviter=session_user_from(inviter))

    assert resent.invitation.invited_by_user_id == inviter.id
    assert resent.raw_token != first.raw_token
    assert await _count_pending_invitations(db_session, first.user.id) == 1


@pytest.mark.integration
async def test_admin_resend_mails_the_inviter_name(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    invited = await invite_to_platform(
        db_session,
        email="pending@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    await reissue_platform_invitation(db_session, invited.user, inviter=session_user_from(inviter))

    result = await db_session.execute(select(OutboundEmail).order_by(col(OutboundEmail.created_at)))
    assert [mail.context["inviter_name"] for mail in result.scalars().all()] == ["Bob Smith", "Bob Smith"]


@pytest.mark.integration
async def test_self_service_reissue_records_no_inviter(
    db_session: AsyncSession, inviter: User, annotator_role: Role
) -> None:
    invited = await invite_to_platform(
        db_session,
        email="pending@example.com",
        role_ids=[annotator_role.id],
        inviter=session_user_from(inviter),
    )

    resent = await reissue_platform_invitation(db_session, invited.user)

    assert resent.invitation.invited_by_user_id is None
