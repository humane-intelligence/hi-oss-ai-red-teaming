"""Integration tests for `scripts.seed_local`.

Role synchronization itself is exercised in
`tests/core/auth/services/test_roles.py`; here we cover the full local seed —
one login per role, the vertical slice it builds, idempotency, and the
non-local refusal guard. `seed` is driven directly against the isolated test
session; `seed_local` (engine + env guard) is only checked for its refusal.
"""

import pytest
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import col

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.annotations.models import MessageFlag
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.roles import SystemRole
from app.core.auth.services.passwords import verify_password
from app.core.config import get_settings
from app.core.conversations.models import Conversation
from app.core.conversations.models import ConversationGroup
from app.core.evaluations.enums import EvaluationGroupAccessLevel
from app.core.evaluations.enums import PublicationStatus
from app.core.evaluations.models import Evaluation
from app.core.evaluations.models import EvaluationAiModel
from app.core.evaluations.models import EvaluationGroup
from app.core.evaluations.models import Scenario
from app.core.evaluations.models import Task
from app.core.reviews.enums import ReviewStatus
from app.core.reviews.models import Review
from scripts.seed_local import GROUP_TITLE
from scripts.seed_local import SEED_LOGINS
from scripts.seed_local import SEED_PASSWORD
from scripts.seed_local import SLM_INFERENCE_ENDPOINT
from scripts.seed_local import SLM_MODEL_ALIAS
from scripts.seed_local import SeedRefusedError
from scripts.seed_local import seed
from scripts.seed_local import seed_local


async def _count(session: AsyncSession, model: type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _user(session: AsyncSession, email: str) -> User | None:
    return (
        await session.execute(
            select(User)
            .options(selectinload(User.roles))  # ty: ignore[invalid-argument-type]
            .where(col(User.email) == email, col(User.deleted_at).is_(None))
        )
    ).scalar_one_or_none()


@pytest.mark.integration
async def test_seed_creates_one_active_login_per_role(db_session: AsyncSession) -> None:
    await seed(db_session, get_settings())

    for role_key, email in SEED_LOGINS.items():
        user = await _user(db_session, email)
        assert user is not None, email
        assert user.status == UserStatus.ACTIVE
        assert user.email_verified_at is not None
        assert user.password is not None
        assert verify_password(SEED_PASSWORD, user.password)
        assert role_key.value in {role.name for role in user.roles}


@pytest.mark.integration
async def test_seed_builds_full_vertical_slice(db_session: AsyncSession) -> None:
    await seed(db_session, get_settings())

    group = (
        await db_session.execute(select(EvaluationGroup).where(col(EvaluationGroup.title) == GROUP_TITLE))
    ).scalar_one()
    assert group.access_level == EvaluationGroupAccessLevel.PUBLIC
    assert group.status == PublicationStatus.PUBLISHED

    assert await _count(db_session, Evaluation) == 1
    assert await _count(db_session, Scenario) == 1
    assert await _count(db_session, Task) == 1
    assert await _count(db_session, EvaluationAiModel) == 1
    assert await _count(db_session, ConversationGroup) == 1
    assert await _count(db_session, Conversation) == 1

    # The seed adds the unassigned self-hosted SLM row alongside the assigned demo model,
    # so there are two AiModels but still one assignment.
    assert await _count(db_session, AiModel) == 2
    slm = (await db_session.execute(select(AiModel).where(col(AiModel.model_alias) == SLM_MODEL_ALIAS))).scalar_one()
    assert slm.provider == ProviderVendor.GENERIC
    assert slm.inference_endpoint == SLM_INFERENCE_ENDPOINT
    assert slm.api_key_encrypted is not None  # credential stored encrypted, never plaintext

    flag = (await db_session.execute(select(MessageFlag))).scalar_one()
    assert flag.red_flagged is True

    review = (await db_session.execute(select(Review))).scalar_one()
    assert review.status == ReviewStatus.PENDING
    annotator = await _user(db_session, SEED_LOGINS[SystemRole.ANNOTATOR])
    red_teamer = await _user(db_session, SEED_LOGINS[SystemRole.RED_TEAMER])
    assert annotator is not None
    assert red_teamer is not None
    assert review.reviewer_id == annotator.id
    assert review.reviewer_id != red_teamer.id


@pytest.mark.integration
async def test_seed_is_idempotent(db_session: AsyncSession) -> None:
    settings = get_settings()
    await seed(db_session, settings)
    await seed(db_session, settings)

    assert await _count(db_session, User) == len(SEED_LOGINS)
    assert await _count(db_session, EvaluationGroup) == 1
    assert await _count(db_session, Evaluation) == 1
    assert await _count(db_session, Scenario) == 1
    assert await _count(db_session, Task) == 1
    assert await _count(db_session, AiModel) == 2  # demo + SLM, not duplicated on re-run
    assert await _count(db_session, EvaluationAiModel) == 1
    assert await _count(db_session, ConversationGroup) == 1
    assert await _count(db_session, Conversation) == 1
    assert await _count(db_session, MessageFlag) == 1
    assert await _count(db_session, Review) == 1

    admin = await _user(db_session, SEED_LOGINS[SystemRole.ADMIN])
    assert admin is not None
    assert {role.name for role in admin.roles} == {SystemRole.ADMIN.value}


@pytest.mark.integration
async def test_seed_local_refuses_non_local_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest pins ENVIRONMENT=test — the entry point must refuse before
    # touching the DB. Stub `build_engine` so a future bug that flips the guard
    # order fails loudly rather than silently connecting.
    def _must_not_be_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("build_engine must not be called when environment != 'local'")

    monkeypatch.setattr("scripts.seed_local.build_engine", _must_not_be_called)

    with pytest.raises(SeedRefusedError):
        await seed_local()
