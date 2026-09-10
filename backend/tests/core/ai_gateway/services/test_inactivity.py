"""Service tests for the model-inactivity sweep — who alerts, who doesn't, and how often."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from sqlmodel import select

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.services import inactivity as inactivity_module
from app.core.ai_gateway.services.ai_models import create_model
from app.core.ai_gateway.services.inactivity import _claim
from app.core.ai_gateway.services.inactivity import alert_inactive_models
from app.core.ai_gateway.services.inactivity import find_inactive_models
from app.core.auth.models import Role
from app.core.auth.models import User
from app.core.auth.models import UserStatus
from app.core.auth.roles import Permission
from app.core.auth.roles import SystemRole
from app.core.auth.services.users import create_user
from app.core.config import Settings
from app.core.email.models import OutboundEmail
from app.core.notifications.models import Notification

_ACTIONABLE_PERMISSIONS = [Permission.MODELS_UPDATE.value, Permission.MODELS_READ.value]


@pytest.fixture
async def admin(db_session: AsyncSession) -> User:
    role = Role(name=SystemRole.ADMIN.value, description="admin", permissions=_ACTIONABLE_PERMISSIONS)
    db_session.add(role)
    await db_session.flush()
    return await create_user(
        db_session, email="admin@example.com", roles=[role], status=UserStatus.ACTIVE, first_name="Ada"
    )


async def _model(
    session: AsyncSession,
    settings: Settings,
    *,
    name: str,
    warmup_enabled: bool = True,
    alert_hours: int | None = 24,
    last_used_at: datetime | None = None,
    last_warmup_at: datetime | None = None,
) -> AiModel:
    model = await create_model(
        session,
        settings,
        name=name,
        model_alias=name,
        provider=ProviderVendor.GENERIC,
        provider_model_id="qwen2.5-0.5b-instruct",
        inference_endpoint="http://slm:8080/v1",
        warmup_enabled=warmup_enabled,
        inactivity_alert_hours=alert_hours,
    )
    model.last_used_at = last_used_at
    model.last_warmup_at = last_warmup_at
    session.add(model)
    await session.flush()
    return model


async def _notifications(session: AsyncSession, model_id: object) -> list[Notification]:
    result = await session.execute(select(Notification).where(col(Notification.object_id) == model_id))
    return list(result.scalars().all())


@pytest.mark.integration
async def test_finds_only_models_past_their_own_threshold(db_session: AsyncSession, app_settings: Settings) -> None:
    now = datetime.now(UTC)
    idle = await _model(db_session, app_settings, name="idle", last_used_at=now - timedelta(hours=30))
    await _model(db_session, app_settings, name="busy", last_used_at=now - timedelta(hours=2))
    # Same 30h of quiet, but its own threshold is a week — not idle yet.
    await _model(db_session, app_settings, name="patient", alert_hours=168, last_used_at=now - timedelta(hours=30))

    found = await find_inactive_models(db_session, now=now)

    assert [model.id for model in found] == [idle.id]


@pytest.mark.integration
async def test_skips_opted_out_disabled_and_non_warmup_models(db_session: AsyncSession, app_settings: Settings) -> None:
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=30)
    await _model(db_session, app_settings, name="no-threshold", alert_hours=None, last_used_at=long_ago)
    await _model(db_session, app_settings, name="no-warmup", warmup_enabled=False, last_used_at=long_ago)
    disabled = await _model(db_session, app_settings, name="disabled", last_used_at=long_ago)
    disabled.disable()
    db_session.add(disabled)
    deleted = await _model(db_session, app_settings, name="deleted", last_used_at=long_ago)
    deleted.deleted_at = now
    db_session.add(deleted)
    await db_session.flush()

    assert await find_inactive_models(db_session, now=now) == []


@pytest.mark.integration
async def test_the_threshold_boundary_is_inclusive(db_session: AsyncSession, app_settings: Settings) -> None:
    # Pins the `>=` at exactly hours*3600 — an off-by-one to `>` would push every alert
    # one sweep interval late.
    now = datetime.now(UTC)
    model = await _model(db_session, app_settings, name="on-the-line", last_used_at=now - timedelta(hours=24))

    found = await find_inactive_models(db_session, now=now)

    assert [found_model.id for found_model in found] == [model.id]


@pytest.mark.integration
async def test_never_used_model_is_measured_from_creation(db_session: AsyncSession, app_settings: Settings) -> None:
    # A registered-but-forgotten endpoint has no `last_used_at` at all; it should still alert.
    model = await _model(db_session, app_settings, name="forgotten", alert_hours=1, last_used_at=None)

    found = await find_inactive_models(db_session, now=datetime.now(UTC) + timedelta(hours=2))

    assert [found_model.id for found_model in found] == [model.id]


@pytest.mark.integration
async def test_alert_notifies_admin_and_stamps_the_episode(
    db_session: AsyncSession, app_settings: Settings, admin: User
) -> None:
    model = await _model(
        db_session, app_settings, name="idle-alert", last_used_at=datetime.now(UTC) - timedelta(hours=30)
    )

    alerted = await alert_inactive_models(db_session)
    await db_session.refresh(model)

    assert alerted == 1
    assert model.inactivity_alerted_at is not None
    notifications = await _notifications(db_session, model.id)
    assert [n.user_id for n in notifications] == [admin.id]
    # `send_email_best_effort` swallows a context/schema mismatch, so without this the mail
    # could silently stop going out and the suite would stay green.
    mails = await db_session.execute(
        select(OutboundEmail).where(col(OutboundEmail.template_name) == "model_inactivity_alert")
    )
    assert [mail.recipient for mail in mails.scalars().all()] == [admin.email]


@pytest.mark.integration
async def test_second_sweep_does_not_realert(db_session: AsyncSession, app_settings: Settings, admin: User) -> None:
    # The claim guard is what makes redelivery and overlapping beat ticks safe.
    model = await _model(
        db_session, app_settings, name="idle-once", last_used_at=datetime.now(UTC) - timedelta(hours=30)
    )

    first = await alert_inactive_models(db_session)
    second = await alert_inactive_models(db_session)

    assert (first, second) == (1, 0)
    assert len(await _notifications(db_session, model.id)) == 1


@pytest.mark.integration
async def test_one_failing_recipient_does_not_starve_the_rest(
    db_session: AsyncSession, app_settings: Settings, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The claim commits before the fan-out, so an unguarded raise would leave the
    # episode permanently half-alerted: stamped, but with nobody told.
    second_role = Role(name="second-admin", description="admin", permissions=_ACTIONABLE_PERMISSIONS)
    db_session.add(second_role)
    await db_session.flush()
    await create_user(
        db_session, email="admin2@example.com", roles=[second_role], status=UserStatus.ACTIVE, first_name="Bob"
    )
    model = await _model(
        db_session, app_settings, name="idle-flaky-fanout", last_used_at=datetime.now(UTC) - timedelta(hours=30)
    )

    real_create = inactivity_module.create_notification
    calls = 0

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(inactivity_module, "create_notification", flaky)

    alerted = await alert_inactive_models(db_session)

    assert alerted == 1
    assert len(await _notifications(db_session, model.id)) == 1


@pytest.mark.integration
async def test_a_mid_transaction_failure_still_reaches_the_remaining_recipients(
    db_session: AsyncSession, app_settings: Settings, admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Raises AFTER the notification flushed, so the rollback is real: it expires every
    # loaded instance, and any ORM attribute access afterwards would raise MissingGreenlet
    # — the loop must run on plain snapshots.
    second_role = Role(name="second-admin", description="admin", permissions=_ACTIONABLE_PERMISSIONS)
    db_session.add(second_role)
    await db_session.flush()
    await create_user(
        db_session, email="admin2@example.com", roles=[second_role], status=UserStatus.ACTIVE, first_name="Bob"
    )
    model = await _model(
        db_session, app_settings, name="idle-mid-tx-fanout", last_used_at=datetime.now(UTC) - timedelta(hours=30)
    )
    # The rollback expires `model` too — the assertion below must not touch the instance.
    model_id = model.id

    real_create = inactivity_module.create_notification
    calls = 0

    async def flaky(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = await real_create(*args, **kwargs)
        if calls == 1:
            raise RuntimeError("boom after flush")
        return result

    monkeypatch.setattr(inactivity_module, "create_notification", flaky)

    alerted = await alert_inactive_models(db_session)

    assert alerted == 1
    assert len(await _notifications(db_session, model_id)) == 1


@pytest.mark.integration
async def test_the_two_capabilities_may_come_from_different_roles(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # The requirement is per user, not per role: update via one role, read via another.
    update_role = Role(name="updater", description="custom", permissions=[Permission.MODELS_UPDATE.value])
    read_role = Role(name="reader", description="custom", permissions=[Permission.MODELS_READ.value])
    db_session.add(update_role)
    db_session.add(read_role)
    await db_session.flush()
    operator = await create_user(
        db_session,
        email="split-roles@example.com",
        roles=[update_role, read_role],
        status=UserStatus.ACTIVE,
        first_name="Sue",
    )
    model = await _model(
        db_session, app_settings, name="idle-split-roles", last_used_at=datetime.now(UTC) - timedelta(hours=30)
    )

    alerted = await alert_inactive_models(db_session)

    assert alerted == 1
    assert [n.user_id for n in await _notifications(db_session, model.id)] == [operator.id]


@pytest.mark.integration
async def test_restoring_a_deleted_model_rearms_the_alert(
    db_session: AsyncSession, app_settings: Settings, admin: User
) -> None:
    # Soft-delete disarms the sweep too (it reads through `live_select`), so bringing the
    # row back is a fresh decision like a re-enable — the stamp must not outlive the delete.
    model = await _model(
        db_session, app_settings, name="idle-restored", last_used_at=datetime.now(UTC) - timedelta(hours=30)
    )

    assert await alert_inactive_models(db_session) == 1
    model.deleted_at = datetime.now(UTC)
    db_session.add(model)
    await db_session.flush()
    model.restore()
    db_session.add(model)
    await db_session.flush()

    assert await alert_inactive_models(db_session) == 1
    assert len(await _notifications(db_session, model.id)) == 2


@pytest.mark.integration
async def test_reenabling_a_disabled_model_rearms_the_alert(
    db_session: AsyncSession, app_settings: Settings, admin: User
) -> None:
    # Disable → re-enable is a fresh decision to keep the model around; the next
    # quiet period is a new episode and must alert again.
    model = await _model(
        db_session, app_settings, name="idle-reenabled", last_used_at=datetime.now(UTC) - timedelta(hours=30)
    )

    assert await alert_inactive_models(db_session) == 1
    model.disable()
    model.enable()
    db_session.add(model)
    await db_session.flush()

    assert await alert_inactive_models(db_session) == 1
    assert len(await _notifications(db_session, model.id)) == 2


@pytest.mark.integration
async def test_a_noop_enable_does_not_rearm_the_episode(
    db_session: AsyncSession, app_settings: Settings, admin: User
) -> None:
    # The console sends `is_disabled: false` on every save, which `update_model` routes
    # through `enable()` — an unrelated edit must not restart an already-alerted episode.
    model = await _model(
        db_session, app_settings, name="idle-edited", last_used_at=datetime.now(UTC) - timedelta(hours=30)
    )

    assert await alert_inactive_models(db_session) == 1
    model.enable()
    db_session.add(model)
    await db_session.flush()

    assert await alert_inactive_models(db_session) == 0
    assert len(await _notifications(db_session, model.id)) == 1


@pytest.mark.integration
async def test_claim_loses_the_row_when_traffic_lands_after_selection(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # A stamp-only CAS would still pass here (the stamp is NULL), alert on a just-used
    # model, and leave the stamp set — swallowing the next genuine idle episode.
    now = datetime.now(UTC)
    model = await _model(db_session, app_settings, name="raced", last_used_at=now - timedelta(hours=30))
    [candidate] = await find_inactive_models(db_session, now=now)
    candidate.last_used_at = now
    db_session.add(candidate)
    await db_session.flush()

    assert await _claim(db_session, model.id, now=now) is False
    await db_session.refresh(model)
    assert model.inactivity_alerted_at is None


@pytest.mark.integration
async def test_claim_skips_a_model_soft_deleted_after_selection(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    now = datetime.now(UTC)
    model = await _model(db_session, app_settings, name="deleted-mid-sweep", last_used_at=now - timedelta(hours=30))
    [candidate] = await find_inactive_models(db_session, now=now)
    candidate.deleted_at = now
    db_session.add(candidate)
    await db_session.flush()

    assert await _claim(db_session, model.id, now=now) is False


@pytest.mark.integration
async def test_alert_is_skipped_when_no_active_admin_exists(db_session: AsyncSession, app_settings: Settings) -> None:
    # Nobody can act on the alert, so the episode stays unclaimed and re-alerts once an admin exists.
    model = await _model(
        db_session, app_settings, name="idle-no-admin", last_used_at=datetime.now(UTC) - timedelta(hours=30)
    )

    alerted = await alert_inactive_models(db_session)
    await db_session.refresh(model)

    assert alerted == 0
    assert model.inactivity_alerted_at is None


@pytest.mark.integration
async def test_recipient_is_any_holder_of_models_update(db_session: AsyncSession, app_settings: Settings) -> None:
    # `models:update` is delegable, so a custom role holding it must be alerted too — the
    # audience is the capability, not the `admin` role name.
    role = Role(name="fleet-operator", description="custom", permissions=_ACTIONABLE_PERMISSIONS)
    db_session.add(role)
    await db_session.flush()
    operator = await create_user(
        db_session, email="operator@example.com", roles=[role], status=UserStatus.ACTIVE, first_name="Grace"
    )
    model = await _model(
        db_session, app_settings, name="idle-custom-role", last_used_at=datetime.now(UTC) - timedelta(hours=30)
    )

    alerted = await alert_inactive_models(db_session)

    assert alerted == 1
    assert [n.user_id for n in await _notifications(db_session, model.id)] == [operator.id]


@pytest.mark.integration
async def test_an_update_only_holder_is_not_alerted(db_session: AsyncSession, app_settings: Settings) -> None:
    # The alert deep-links to the model page, gated on `models:read` — an update-only
    # role would get a mail whose link lands on NotAuthorized.
    role = Role(name="update-only", description="custom", permissions=[Permission.MODELS_UPDATE.value])
    db_session.add(role)
    await db_session.flush()
    await create_user(
        db_session, email="update-only@example.com", roles=[role], status=UserStatus.ACTIVE, first_name="Uma"
    )
    model = await _model(
        db_session, app_settings, name="idle-update-only", last_used_at=datetime.now(UTC) - timedelta(hours=30)
    )

    alerted = await alert_inactive_models(db_session)

    assert alerted == 0
    assert await _notifications(db_session, model.id) == []


@pytest.mark.integration
async def test_alert_names_the_expensive_case_when_the_model_is_still_being_warmed(
    db_session: AsyncSession, app_settings: Settings, admin: User
) -> None:
    # Warmed recently but never messaged = something keeps waking the endpoint for nobody.
    # That is the case actually costing money, and the alert has to say so.
    now = datetime.now(UTC)
    model = await _model(
        db_session,
        app_settings,
        name="idle-but-warmed",
        last_used_at=now - timedelta(hours=30),
        last_warmup_at=now - timedelta(minutes=5),
    )

    await alert_inactive_models(db_session)

    [notification] = await _notifications(db_session, model.id)
    assert "still being warmed" in (notification.description or "")


@pytest.mark.integration
async def test_alert_says_nothing_is_warming_an_abandoned_model(
    db_session: AsyncSession, app_settings: Settings, admin: User
) -> None:
    # No warmups either: a scale-to-zero endpoint is already asleep, so this one is
    # registry clutter rather than a live cost — different message, different urgency.
    model = await _model(
        db_session,
        app_settings,
        name="idle-and-cold",
        last_used_at=datetime.now(UTC) - timedelta(hours=30),
        last_warmup_at=None,
    )

    await alert_inactive_models(db_session)

    [notification] = await _notifications(db_session, model.id)
    assert "nothing is warming it" in (notification.description or "")


@pytest.mark.integration
async def test_a_warmup_older_than_the_quiet_window_does_not_count_as_still_warmed(
    db_session: AsyncSession, app_settings: Settings, admin: User
) -> None:
    # Regression on wording: with a fixed 1h window this said "nothing is warming it" while the
    # mail printed a warmup 70 minutes old. The window is the alerted quiet period itself, so a
    # warmup that predates it is genuinely past — no contradiction to read.
    now = datetime.now(UTC)
    model = await _model(
        db_session,
        app_settings,
        name="warmed-before-the-window",
        alert_hours=1,
        last_used_at=now - timedelta(hours=3),
        last_warmup_at=now - timedelta(hours=2),
    )

    await alert_inactive_models(db_session)

    [notification] = await _notifications(db_session, model.id)
    assert "nothing is warming it" in (notification.description or "")


@pytest.mark.integration
async def test_the_window_scales_with_a_long_threshold(
    db_session: AsyncSession, app_settings: Settings, admin: User
) -> None:
    # Same 2h-old warmup, week-long threshold: it falls *inside* the quiet window, so something
    # is still waking a model nobody messages — the expensive case, and the copy must say so.
    now = datetime.now(UTC)
    model = await _model(
        db_session,
        app_settings,
        name="warmed-inside-a-long-window",
        alert_hours=168,
        last_used_at=now - timedelta(days=10),
        last_warmup_at=now - timedelta(hours=2),
    )

    await alert_inactive_models(db_session)

    [notification] = await _notifications(db_session, model.id)
    assert "still being warmed" in (notification.description or "")
