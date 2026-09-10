"""Integration tests for the platform-settings singleton service."""

from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.licenses.catalog import curated_license_id
from app.core.platform_settings.models import PLATFORM_SETTINGS_ID
from app.core.platform_settings.service import get_platform_settings
from app.core.platform_settings.service import update_platform_settings


@pytest.mark.integration
async def test_get_returns_transient_env_default_when_no_row(db_session: AsyncSession) -> None:
    settings = await get_platform_settings(db_session)

    assert settings.default_license_id == curated_license_id("CC-BY-4.0")
    assert settings.invite_only is False
    assert settings.email_verification_ttl_hours == 24
    # The shipped password policy is the pre-knob behaviour: floor 8, no character classes.
    assert settings.password_min_length == 8
    assert settings.password_require_uppercase is False
    assert settings.password_require_digit is False
    assert settings.password_require_symbol is False
    assert settings.password_reset_cooldown_seconds == 60
    assert settings.password_reset_max_per_day == 5
    # Transient — never persisted, so no server-stamped timestamps.
    assert settings.updated_at is None
    assert await db_session.get(type(settings), PLATFORM_SETTINGS_ID) is None


@pytest.mark.integration
async def test_get_seeds_from_settings(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.core.platform_settings.service.get_settings",
        lambda: SimpleNamespace(platform_default_data_license="CC0-1.0", email_verification_ttl_hours=72),
    )

    settings = await get_platform_settings(db_session)

    assert settings.default_license_id == curated_license_id("CC0-1.0")
    assert settings.email_verification_ttl_hours == 72


@pytest.mark.integration
async def test_update_materializes_row_then_reads_back(db_session: AsyncSession) -> None:
    license_id = curated_license_id("CC0-1.0")
    updated = await update_platform_settings(db_session, default_license_id=license_id)
    reloaded = await get_platform_settings(db_session)

    assert updated.id == PLATFORM_SETTINGS_ID
    assert updated.default_license_id == license_id
    assert reloaded.default_license_id == license_id
    assert reloaded.updated_at is not None


@pytest.mark.integration
async def test_update_is_idempotent_on_single_row(db_session: AsyncSession) -> None:
    await update_platform_settings(db_session, default_license_id=curated_license_id("CC0-1.0"))
    await update_platform_settings(db_session, default_license_id=curated_license_id("CC-BY-SA-4.0"))

    reloaded = await get_platform_settings(db_session)
    assert reloaded.default_license_id == curated_license_id("CC-BY-SA-4.0")
    assert reloaded.id == PLATFORM_SETTINGS_ID


@pytest.mark.integration
async def test_update_returns_fresh_value_after_prior_read_in_same_session(db_session: AsyncSession) -> None:
    # Regression: a read (session.get) pins the row in the identity map; the subsequent
    # upsert must return the freshly written value, not the stale cached instance.
    await update_platform_settings(db_session, default_license_id=curated_license_id("CC0-1.0"))
    pinned = await get_platform_settings(db_session)
    assert pinned.default_license_id == curated_license_id("CC0-1.0")

    updated = await update_platform_settings(db_session, default_license_id=curated_license_id("CC-BY-SA-4.0"))
    reloaded = await get_platform_settings(db_session)

    assert updated.default_license_id == curated_license_id("CC-BY-SA-4.0")
    assert reloaded.default_license_id == curated_license_id("CC-BY-SA-4.0")


@pytest.mark.integration
async def test_first_write_of_invite_only_materializes_effective_license_default(db_session: AsyncSession) -> None:
    # The INSERT arm must carry the *effective* default license, not a DDL default —
    # a first-ever PATCH of the other knob alone may materialize the row.
    updated = await update_platform_settings(db_session, invite_only=True)

    assert updated.invite_only is True
    assert updated.default_license_id == curated_license_id("CC-BY-4.0")


@pytest.mark.integration
async def test_update_one_knob_leaves_the_others_unchanged(db_session: AsyncSession) -> None:
    await update_platform_settings(db_session, default_license_id=curated_license_id("CC0-1.0"))

    await update_platform_settings(db_session, invite_only=True)
    await update_platform_settings(db_session, email_verification_ttl_hours=48)
    reloaded = await get_platform_settings(db_session)

    assert reloaded.default_license_id == curated_license_id("CC0-1.0")
    assert reloaded.invite_only is True
    assert reloaded.email_verification_ttl_hours == 48


@pytest.mark.integration
async def test_first_write_of_a_password_knob_materializes_the_rest_at_effective_values(
    db_session: AsyncSession,
) -> None:
    # Same invariant as the licence case, for the knobs added later: the INSERT arm is built from
    # the derived knob list, so a first write of one must not leave the siblings on DDL defaults
    # that happen to differ from the effective ones.
    await update_platform_settings(db_session, email_verification_ttl_hours=48)
    updated = await update_platform_settings(db_session, password_require_digit=True)

    assert updated.password_require_digit is True
    assert updated.password_min_length == 8
    assert updated.password_reset_cooldown_seconds == 60
    assert updated.password_reset_max_per_day == 5
    assert updated.email_verification_ttl_hours == 48


@pytest.mark.integration
async def test_update_both_knobs_at_once(db_session: AsyncSession) -> None:
    updated = await update_platform_settings(
        db_session, default_license_id=curated_license_id("CC-BY-SA-4.0"), invite_only=True
    )

    assert updated.default_license_id == curated_license_id("CC-BY-SA-4.0")
    assert updated.invite_only is True
