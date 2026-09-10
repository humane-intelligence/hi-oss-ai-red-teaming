"""Service tests for `invite_to_group` and the onboarding accept flow.

Covers the two branches (immediate assign for an active account, token issue for
a new/onboarding one), multi-role invites, reconcile-on-reinvite, the deactivated
conflict, the scoped revoke that keeps platform and group invitations independent,
and that roles are pre-assigned at invite time and survive the accept (which only
activates the account). The service persists only — the mail comes back as
`result.email_spec` for the caller to dispatch, so email assertions here are
spec assertions.
"""

from datetime import date
from unittest.mock import MagicMock
from urllib.parse import parse_qs
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.auth.models import Invitation
from app.core.auth.models import InvitationStatus
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.object_roles.registry import ObjectType
from app.core.auth.object_roles.service import held_roles
from app.core.auth.schemas import SessionUser
from app.core.auth.services.invitations import accept_invitation
from app.core.auth.services.invitations import invite_to_platform
from app.core.auth.services.users import create_user
from app.core.auth.services.users import get_user_by_email
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.services.group_invitations import invite_to_group
from app.core.exceptions import BadRequestError
from app.core.exceptions import ConflictError
from app.core.exceptions import GoneError
from tests.conftest import session_user_from

pytestmark = pytest.mark.integration

_GROUP = ObjectType.EVALUATION_GROUP


@pytest.fixture(autouse=True)
def _celery_enqueue_stub(celery_enqueue_stub: MagicMock) -> None:
    """Every invite path here dispatches mail, so the shared spy is module-wide."""


@pytest_asyncio.fixture
async def inviter(db_session: AsyncSession, system_roles: dict[str, Role]) -> User:
    return await create_user(
        db_session,
        email="inviter@example.com",
        first_name="Olive",
        last_name="Owner",
        status=UserStatus.ACTIVE,
        roles=[system_roles["owner"]],
    )


@pytest_asyncio.fixture
async def group(db_session: AsyncSession, inviter: User) -> EvaluationGroup:
    group = EvaluationGroup(
        title="Spring Engagement",
        description="A red-teaming engagement.",
        created_by_id=inviter.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        start_date=date(2026, 3, 1),
    )
    db_session.add(group)
    await db_session.flush()
    await db_session.refresh(group)
    return group


def _inviter(inviter: User) -> SessionUser:
    return session_user_from(inviter)


def _token_from(result) -> str:
    secret_context = result.email_spec.secret_context
    assert secret_context is not None
    return parse_qs(urlparse(secret_context["accept_url"]).query)["token"][0]


async def test_invite_new_user_creates_invited_user_and_assigns_roles(
    db_session: AsyncSession, inviter: User, group: EvaluationGroup, system_roles: dict[str, Role]
) -> None:
    result = await invite_to_group(
        db_session,
        group=group,
        inviter=_inviter(inviter),
        email="ada@example.com",
        role_ids=[system_roles["red_teamer"].id],
    )

    assert result.outcome == "invited"
    assert result.expires_at is not None

    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    assert user.status is UserStatus.INVITED
    assert {role.name for role in user.roles} == {"red_teamer"}  # default global role

    # The token carries the scope only — the role lives on the assignment table,
    # pre-assigned (but inert) on the still-invited account.
    invitation = (await db_session.execute(Invitation.live_select())).scalar_one()
    assert invitation.object_type is ObjectType.EVALUATION_GROUP
    assert invitation.object_id == group.id
    held = await held_roles(db_session, _GROUP, group.id, user.id)
    assert {role.name for role in held} == {"red_teamer"}


async def test_invite_new_user_multiple_roles(
    db_session: AsyncSession, inviter: User, group: EvaluationGroup, system_roles: dict[str, Role]
) -> None:
    result = await invite_to_group(
        db_session,
        group=group,
        inviter=_inviter(inviter),
        email="ada@example.com",
        role_ids=[system_roles["viewer"].id, system_roles["annotator"].id],
    )

    assert {role.name for role in result.roles} == {"viewer", "annotator"}
    user = await get_user_by_email(db_session, "ada@example.com")
    assert user is not None
    held = await held_roles(db_session, _GROUP, group.id, user.id)
    assert {role.name for role in held} == {"viewer", "annotator"}


async def test_invite_new_user_returns_invitation_email_spec(
    db_session: AsyncSession, inviter: User, group: EvaluationGroup, system_roles: dict[str, Role]
) -> None:
    result = await invite_to_group(
        db_session,
        group=group,
        inviter=_inviter(inviter),
        email="ada@example.com",
        role_ids=[system_roles["red_teamer"].id],
    )

    spec = result.email_spec
    assert spec.template == "evaluation_group_invitation"
    assert spec.to == "ada@example.com"
    assert spec.context["group_title"] == "Spring Engagement"
    # The bearer token rides secret_context (kept out of the persisted audit row).
    assert "accept_url" not in spec.context
    assert spec.secret_context is not None
    assert "/invite/accept?token=" in spec.secret_context["accept_url"]


async def test_invite_active_user_assigns_roles_immediately(
    db_session: AsyncSession, inviter: User, group: EvaluationGroup, system_roles: dict[str, Role]
) -> None:
    active = await create_user(
        db_session,
        email="ada@example.com",
        status=UserStatus.ACTIVE,
        roles=[system_roles["red_teamer"]],
    )

    result = await invite_to_group(
        db_session,
        group=group,
        inviter=_inviter(inviter),
        email="ada@example.com",
        role_ids=[system_roles["viewer"].id],
    )

    assert result.outcome == "assigned"
    held = await held_roles(db_session, _GROUP, group.id, active.id)
    assert {role.name for role in held} == {"viewer"}
    # No token for an active account.
    assert (await db_session.execute(Invitation.live_select())).first() is None
    assert result.email_spec.template == "evaluation_group_member_added"
    assert result.email_spec.to == "ada@example.com"


async def test_reinvite_member_reconciles_roles(
    db_session: AsyncSession, inviter: User, group: EvaluationGroup, system_roles: dict[str, Role]
) -> None:
    # Re-inviting an existing member reconciles their role set wholesale rather
    # than raising — the invite means "this user should hold exactly these roles".
    active = await create_user(
        db_session,
        email="ada@example.com",
        status=UserStatus.ACTIVE,
        roles=[system_roles["red_teamer"]],
    )
    await invite_to_group(
        db_session,
        group=group,
        inviter=_inviter(inviter),
        email=active.email,
        role_ids=[system_roles["viewer"].id],
    )

    result = await invite_to_group(
        db_session,
        group=group,
        inviter=_inviter(inviter),
        email=active.email,
        role_ids=[system_roles["annotator"].id],
    )

    assert result.outcome == "assigned"
    held = await held_roles(db_session, _GROUP, group.id, active.id)
    assert {role.name for role in held} == {"annotator"}


async def test_invite_inactive_user_conflicts(
    db_session: AsyncSession, inviter: User, group: EvaluationGroup, system_roles: dict[str, Role]
) -> None:
    await create_user(
        db_session,
        email="ada@example.com",
        status=UserStatus.INACTIVE,
        roles=[system_roles["red_teamer"]],
    )

    with pytest.raises(ConflictError, match="deactivated"):
        await invite_to_group(
            db_session,
            group=group,
            inviter=_inviter(inviter),
            email="ada@example.com",
            role_ids=[system_roles["viewer"].id],
        )


async def test_accept_activates_account_with_preassigned_roles(
    db_session: AsyncSession, inviter: User, group: EvaluationGroup, system_roles: dict[str, Role]
) -> None:
    result = await invite_to_group(
        db_session,
        group=group,
        inviter=_inviter(inviter),
        email="ada@example.com",
        role_ids=[system_roles["annotator"].id],
    )
    assert result.outcome == "invited"

    # Roles are held already (inert) before the account activates.
    invited = await get_user_by_email(db_session, "ada@example.com")
    assert invited is not None
    held_before = await held_roles(db_session, _GROUP, group.id, invited.id)
    assert {role.name for role in held_before} == {"annotator"}

    # Recover the raw token from the spec's accept URL — it never hits the DB.
    token = _token_from(result)

    user = await accept_invitation(
        db_session,
        raw_token=token,
        password=SecretStr("supersecret-12345"),
        first_name="Ada",
        last_name=None,
    )

    assert user.status is UserStatus.ACTIVE
    held = await held_roles(db_session, _GROUP, group.id, user.id)
    assert {role.name for role in held} == {"annotator"}


async def test_invite_pending_selfsignup_user_issues_token_and_survives_accept(
    db_session: AsyncSession, inviter: User, group: EvaluationGroup, system_roles: dict[str, Role]
) -> None:
    # A self-signup account awaiting email verification can't authenticate yet,
    # so it takes the `invited` branch — no duplicate account, and the accept
    # (which re-sets the password) activates it with the pre-assigned roles.
    pending = await create_user(
        db_session,
        email="ada@example.com",
        status=UserStatus.PENDING,
        password=SecretStr("selfsignup-secret-1"),
        roles=[system_roles["red_teamer"]],
    )

    result = await invite_to_group(
        db_session,
        group=group,
        inviter=_inviter(inviter),
        email="ada@example.com",
        role_ids=[system_roles["viewer"].id],
    )

    assert result.outcome == "invited"
    assert result.user_id == pending.id

    token = _token_from(result)

    user = await accept_invitation(
        db_session,
        raw_token=token,
        password=SecretStr("supersecret-12345"),
        first_name="Ada",
        last_name=None,
    )

    assert user.id == pending.id
    assert user.status is UserStatus.ACTIVE
    held = await held_roles(db_session, _GROUP, group.id, user.id)
    assert {role.name for role in held} == {"viewer"}


async def test_group_invite_does_not_revoke_platform_invitation(
    db_session: AsyncSession, inviter: User, group: EvaluationGroup, system_roles: dict[str, Role]
) -> None:
    # A platform invitation (object_type NULL) and a group invitation are scoped
    # independently — issuing the group one must leave the platform one live.
    platform = await invite_to_platform(
        db_session,
        email="ada@example.com",
        role_ids=[system_roles["red_teamer"].id],
        inviter=_inviter(inviter),
        send_side_effects=False,
    )

    await invite_to_group(
        db_session,
        group=group,
        inviter=_inviter(inviter),
        email="ada@example.com",
        role_ids=[system_roles["viewer"].id],
    )

    refreshed = await db_session.get(Invitation, platform.invitation.id)
    assert refreshed is not None
    assert refreshed.status is InvitationStatus.PENDING

    group_invites = (
        (
            await db_session.execute(
                Invitation.live_select().where(col(Invitation.object_type) == ObjectType.EVALUATION_GROUP)
            )
        )
        .scalars()
        .all()
    )
    assert len(group_invites) == 1


async def test_invite_non_assignable_role_rejected(
    db_session: AsyncSession, inviter: User, group: EvaluationGroup, system_roles: dict[str, Role]
) -> None:
    with pytest.raises(BadRequestError):
        await invite_to_group(
            db_session,
            group=group,
            inviter=_inviter(inviter),
            email="ada@example.com",
            role_ids=[system_roles["admin"].id],
        )

    # Nothing persisted on the rejected path.
    assert (await db_session.execute(Invitation.live_select())).first() is None
    assert (await db_session.execute(User.live_select().where(col(User.email) == "ada@example.com"))).first() is None


async def test_invite_unknown_role_rejected(db_session: AsyncSession, inviter: User, group: EvaluationGroup) -> None:
    with pytest.raises(BadRequestError):
        await invite_to_group(
            db_session,
            group=group,
            inviter=_inviter(inviter),
            email="ada@example.com",
            role_ids=[uuid4()],
        )


async def test_second_live_token_cannot_reactivate_account(
    db_session: AsyncSession, inviter: User, group: EvaluationGroup, system_roles: dict[str, Role]
) -> None:
    # Invites are scope-isolated, so a new email invited to two groups holds two
    # live tokens. Accepting one activates the account; the other must NOT be a
    # standing unauthenticated password reset on the now-active account.
    group_b = EvaluationGroup(
        title="Autumn Engagement",
        description="A second engagement.",
        created_by_id=inviter.id,
        access_level=EvaluationGroupAccessLevel.INVITATION_ONLY,
        status=PublicationStatus.PUBLISHED,
        start_date=date(2026, 9, 1),
    )
    db_session.add(group_b)
    await db_session.flush()

    tokens = []
    for target in (group, group_b):
        result = await invite_to_group(
            db_session,
            group=target,
            inviter=_inviter(inviter),
            email="ada@example.com",
            role_ids=[system_roles["viewer"].id],
        )
        tokens.append(_token_from(result))
    assert len(tokens) == 2

    activated = await accept_invitation(
        db_session,
        raw_token=tokens[0],
        password=SecretStr("supersecret-12345"),
        first_name="Ada",
        last_name=None,
    )
    assert activated.status is UserStatus.ACTIVE

    with pytest.raises(GoneError):
        await accept_invitation(
            db_session,
            raw_token=tokens[1],
            password=SecretStr("attacker-chosen-pw-1"),
            first_name="Mallory",
            last_name=None,
        )
