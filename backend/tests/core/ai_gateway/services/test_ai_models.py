"""Integration tests for `app.core.ai_gateway.services.ai_models` — service layer over a real DB."""

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.crypto import decrypt_secret
from app.core.ai_gateway.enums import Modality
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.filters import AiModelFilters
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.schemas import AiModelUpdateChanges
from app.core.ai_gateway.services.ai_models import MissingInferenceEndpointError
from app.core.ai_gateway.services.ai_models import _duplicate_conflict
from app.core.ai_gateway.services.ai_models import clear_api_key
from app.core.ai_gateway.services.ai_models import create_model
from app.core.ai_gateway.services.ai_models import get_model
from app.core.ai_gateway.services.ai_models import get_model_by_name
from app.core.ai_gateway.services.ai_models import list_labels
from app.core.ai_gateway.services.ai_models import list_models
from app.core.ai_gateway.services.ai_models import set_api_key
from app.core.ai_gateway.services.ai_models import soft_delete_model
from app.core.ai_gateway.services.ai_models import update_model
from app.core.config import Settings
from app.core.config import get_settings
from app.core.exceptions import ConflictError
from app.core.exceptions import NotFoundError
from app.core.restore import restore_cutoff

# These suites exercise the live set; the window is inert but the services now
# require it explicitly, as the routes derive it.
_CUTOFF = restore_cutoff(get_settings())


@pytest.mark.integration
async def test_create_model_persists_row_without_api_key(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="Sonnet",
        model_alias="sonnet",
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id="claude-3-5-sonnet-20240620",
    )

    assert model.id is not None
    assert model.provider is ProviderVendor.ANTHROPIC
    assert model.input_modalities == [Modality.TEXT]
    assert model.output_modalities == [Modality.TEXT]
    assert model.parameters == {}
    assert model.extras == {}
    assert model.is_disabled is False
    assert model.disabled_at is None
    assert model.api_key_encrypted is None


@pytest.mark.integration
async def test_create_model_encrypts_api_key(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="With Key",
        model_alias="with-key",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        api_key=SecretStr("sk-very-secret"),
    )

    ciphertext = model.api_key_encrypted
    assert ciphertext is not None
    assert "sk-very-secret" not in ciphertext
    assert decrypt_secret(ciphertext, app_settings) == "sk-very-secret"


@pytest.mark.integration
async def test_create_model_rejects_duplicate_name(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="dup",
        model_alias="alias-a",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    with pytest.raises(ConflictError) as exc_info:
        await create_model(
            db_session,
            app_settings,
            name="dup",
            model_alias="alias-b",
            provider=ProviderVendor.OPENAI,
            provider_model_id="gpt-4",
        )
    assert exc_info.value.errors is not None
    (error,) = exc_info.value.errors
    assert (error.loc, error.type) == (["body", "name"], "duplicate_name")


@pytest.mark.integration
async def test_create_model_rejects_duplicate_name_different_provider(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    """Uniqueness is on name alone: a second provider does not excuse a repeated name."""
    await create_model(
        db_session,
        app_settings,
        name="shared",
        model_alias="alias-a",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    with pytest.raises(ConflictError) as exc_info:
        await create_model(
            db_session,
            app_settings,
            name="shared",
            model_alias="alias-b",
            provider=ProviderVendor.ANTHROPIC,
            provider_model_id="claude-3",
        )
    assert exc_info.value.errors is not None
    (error,) = exc_info.value.errors
    assert (error.loc, error.type) == (["body", "name"], "duplicate_name")


@pytest.mark.integration
async def test_get_model_by_name_resolves_business_key(db_session: AsyncSession, app_settings: Settings) -> None:
    created = await create_model(
        db_session,
        app_settings,
        name="Sonnet",
        model_alias="sonnet",
        provider=ProviderVendor.ANTHROPIC,
        provider_model_id="claude-3",
    )

    found = await get_model_by_name(db_session, name="Sonnet")
    assert found.id == created.id
    with pytest.raises(NotFoundError):
        await get_model_by_name(db_session, name="Ghost")


@pytest.mark.integration
async def test_get_model_by_name_includes_disabled(db_session: AsyncSession, app_settings: Settings) -> None:
    """Key management targets disabled models too, so the lookup must surface them."""
    created = await create_model(
        db_session,
        app_settings,
        name="Off",
        model_alias="off",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        is_disabled=True,
    )

    found = await get_model_by_name(db_session, name="Off")
    assert found.id == created.id
    assert found.is_disabled is True


@pytest.mark.integration
async def test_create_model_rejects_duplicate_model_alias(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="alpha",
        model_alias="dup",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    with pytest.raises(ConflictError) as exc_info:
        await create_model(
            db_session,
            app_settings,
            name="beta",
            model_alias="dup",
            provider=ProviderVendor.OPENAI,
            provider_model_id="gpt-4",
        )
    assert exc_info.value.errors is not None
    (error,) = exc_info.value.errors
    assert (error.loc, error.type) == (["body", "model_alias"], "duplicate_model_alias")


_GENERIC_DUPLICATE_DETAIL = "A model with this name, or this model_alias, already exists."


def _unique_violation_orig(constraint_name: str | None) -> Exception:
    driver_exc = Exception("unique violation")
    driver_exc.constraint_name = constraint_name  # ty: ignore[unresolved-attribute]
    orig = Exception("wrapped")
    orig.__cause__ = driver_exc
    orig.sqlstate = "23505"  # ty: ignore[unresolved-attribute]
    return orig


@pytest.mark.unit
@pytest.mark.parametrize(
    "orig",
    [
        pytest.param(None, id="orig_is_none"),
        pytest.param(_unique_violation_orig(None), id="constraint_name_is_none"),
        pytest.param(_unique_violation_orig("ix_unrelated_table_column"), id="unrecognized_constraint"),
    ],
)
def test_duplicate_conflict_falls_back_to_generic_message(orig: Exception | None) -> None:
    conflict = _duplicate_conflict(IntegrityError("INSERT ...", {}, orig))  # ty: ignore[invalid-argument-type]

    assert conflict.detail == _GENERIC_DUPLICATE_DETAIL
    assert conflict.errors is None


@pytest.mark.unit
def test_duplicate_conflict_ignores_non_unique_violation_with_matching_constraint_name() -> None:
    # A CHECK/FK constraint lives in a different Postgres namespace than an index,
    # so it can share a literal name with `ix_ai_models_name` without either
    # creation failing — the sqlstate gate is what keeps this off the name branch.
    driver_exc = Exception("check violation")
    driver_exc.constraint_name = "ix_ai_models_name"  # ty: ignore[unresolved-attribute]
    orig = Exception("wrapped")
    orig.__cause__ = driver_exc
    orig.sqlstate = "23514"  # ty: ignore[unresolved-attribute]

    conflict = _duplicate_conflict(IntegrityError("INSERT ...", {}, orig))

    assert conflict.detail == _GENERIC_DUPLICATE_DETAIL
    assert conflict.errors is None


@pytest.mark.integration
async def test_get_model_returns_live_row(db_session: AsyncSession, app_settings: Settings) -> None:
    created = await create_model(
        db_session,
        app_settings,
        name="get-me",
        model_alias="get-me",
        provider=ProviderVendor.COHERE,
        provider_model_id="command-r",
    )

    fetched = await get_model(db_session, created.id)

    assert fetched.id == created.id


@pytest.mark.integration
async def test_get_model_raises_not_found_for_soft_deleted_row(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    created = await create_model(
        db_session,
        app_settings,
        name="gone",
        model_alias="gone",
        provider=ProviderVendor.COHERE,
        provider_model_id="command-r",
    )
    await soft_delete_model(db_session, created, by_id=uuid4())

    with pytest.raises(NotFoundError):
        await get_model(db_session, created.id)


@pytest.mark.integration
async def test_list_models_skips_soft_deleted_rows(db_session: AsyncSession, app_settings: Settings) -> None:
    keeper = await create_model(
        db_session,
        app_settings,
        name="keeper",
        model_alias="keeper",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )
    tombstoned = await create_model(
        db_session,
        app_settings,
        name="tombstoned",
        model_alias="tombstoned",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )
    await soft_delete_model(db_session, tombstoned, by_id=uuid4())

    items, total = await list_models(db_session, filters=AiModelFilters(), limit=20, offset=0, deleted_cutoff=_CUTOFF)

    assert total == 1
    assert [m.id for m in items] == [keeper.id]


@pytest.mark.integration
async def test_list_models_excludes_disabled_by_default(db_session: AsyncSession, app_settings: Settings) -> None:
    enabled = await create_model(
        db_session,
        app_settings,
        name="enabled",
        model_alias="enabled",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )
    await create_model(
        db_session,
        app_settings,
        name="disabled",
        model_alias="disabled",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        is_disabled=True,
    )

    items, total = await list_models(db_session, filters=AiModelFilters(), limit=20, offset=0, deleted_cutoff=_CUTOFF)

    assert total == 1
    assert [m.id for m in items] == [enabled.id]


@pytest.mark.integration
async def test_list_models_includes_disabled_when_flag_set(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="enabled",
        model_alias="enabled",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )
    await create_model(
        db_session,
        app_settings,
        name="disabled",
        model_alias="disabled",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        is_disabled=True,
    )

    items, total = await list_models(
        db_session, filters=AiModelFilters(include_disabled=True), limit=20, offset=0, deleted_cutoff=_CUTOFF
    )

    assert total == 2
    assert {m.name for m in items} == {"enabled", "disabled"}


@pytest.mark.integration
async def test_get_model_hides_disabled_when_excluded(db_session: AsyncSession, app_settings: Settings) -> None:
    created = await create_model(
        db_session,
        app_settings,
        name="hidden",
        model_alias="hidden",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        is_disabled=True,
    )

    with pytest.raises(NotFoundError):
        await get_model(db_session, created.id, include_disabled=False)

    fetched = await get_model(db_session, created.id, include_disabled=True)
    assert fetched.id == created.id


@pytest.mark.integration
async def test_update_model_only_writes_set_fields(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="original",
        model_alias="orig-alias",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        parameters={"temperature": 0.5},
    )

    changes = AiModelUpdateChanges.model_validate({"name": "renamed", "is_disabled": True})
    updated = await update_model(db_session, model, changes)

    assert updated.name == "renamed"
    assert updated.is_disabled is True
    assert updated.disabled_at is not None
    # Untouched fields stay.
    assert updated.model_alias == "orig-alias"
    assert updated.parameters == {"temperature": 0.5}


@pytest.mark.integration
async def test_update_model_re_enable_clears_disabled_at(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="toggle",
        model_alias="toggle",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        is_disabled=True,
    )
    assert model.disabled_at is not None

    await update_model(db_session, model, AiModelUpdateChanges.model_validate({"is_disabled": False}))

    assert model.is_disabled is False
    assert model.disabled_at is None


@pytest.mark.integration
async def test_update_model_threshold_reoptin_rearms_the_alert(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # Silencing by nulling the threshold leaves the episode stamp; re-opting in is a
    # fresh decision — a stale stamp must not suppress every future alert.
    model = await create_model(
        db_session,
        app_settings,
        name="reoptin",
        model_alias="reoptin",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        warmup_enabled=True,
    )
    model.inactivity_alerted_at = datetime.now(UTC)
    db_session.add(model)
    await db_session.flush()

    await update_model(db_session, model, AiModelUpdateChanges.model_validate({"inactivity_alert_hours": 48}))

    assert model.inactivity_alerted_at is None


@pytest.mark.integration
async def test_update_model_threshold_tuning_keeps_the_episode(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # Value → value is tuning, not a re-opt-in: the already-announced episode stands.
    alerted_at = datetime.now(UTC)
    model = await create_model(
        db_session,
        app_settings,
        name="tuning",
        model_alias="tuning",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        warmup_enabled=True,
        inactivity_alert_hours=24,
    )
    model.inactivity_alerted_at = alerted_at
    db_session.add(model)
    await db_session.flush()

    await update_model(db_session, model, AiModelUpdateChanges.model_validate({"inactivity_alert_hours": 72}))

    assert model.inactivity_alerted_at == alerted_at


@pytest.mark.integration
async def test_update_model_warmup_reenable_rearms_the_alert(db_session: AsyncSession, app_settings: Settings) -> None:
    # Unchecking warm-up is the console's silence gesture (it keeps the stored threshold),
    # so re-checking it re-arms — `warmup_enabled` disarms the sweep exactly like the threshold.
    model = await create_model(
        db_session,
        app_settings,
        name="warmup-reoptin",
        model_alias="warmup-reoptin",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        warmup_enabled=False,
        inactivity_alert_hours=24,
    )
    model.inactivity_alerted_at = datetime.now(UTC)
    db_session.add(model)
    await db_session.flush()

    await update_model(db_session, model, AiModelUpdateChanges.model_validate({"warmup_enabled": True}))

    assert model.inactivity_alerted_at is None


@pytest.mark.integration
async def test_update_model_noop_warmup_save_keeps_the_episode(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # The console sends `warmup_enabled` on every save; an unrelated edit must not restart
    # an already-announced episode (same rule as the no-op `enable()`).
    alerted_at = datetime.now(UTC)
    model = await create_model(
        db_session,
        app_settings,
        name="warmup-noop",
        model_alias="warmup-noop",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        warmup_enabled=True,
        inactivity_alert_hours=24,
    )
    model.inactivity_alerted_at = alerted_at
    db_session.add(model)
    await db_session.flush()

    await update_model(
        db_session, model, AiModelUpdateChanges.model_validate({"name": "renamed", "warmup_enabled": True})
    )

    assert model.inactivity_alerted_at == alerted_at


@pytest.mark.integration
async def test_update_model_rejects_conflicting_name(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="taken",
        model_alias="taken",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )
    other = await create_model(
        db_session,
        app_settings,
        name="rename-me",
        model_alias="rename-me",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    with pytest.raises(ConflictError) as exc_info:
        await update_model(db_session, other, AiModelUpdateChanges.model_validate({"name": "taken"}))
    assert exc_info.value.errors is not None
    (error,) = exc_info.value.errors
    assert (error.loc, error.type) == (["body", "name"], "duplicate_name")


@pytest.mark.integration
async def test_set_api_key_encrypts_and_replaces_previous_value(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="rotating",
        model_alias="rotating",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        api_key=SecretStr("first"),
    )
    original_ciphertext = model.api_key_encrypted

    await set_api_key(db_session, app_settings, model, SecretStr("second"))

    rotated = model.api_key_encrypted
    assert rotated is not None
    assert rotated != original_ciphertext
    assert decrypt_secret(rotated, app_settings) == "second"


@pytest.mark.integration
async def test_clear_api_key_removes_credential(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="clearing",
        model_alias="clearing",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        api_key=SecretStr("sk"),
    )

    await clear_api_key(db_session, model)

    assert model.api_key_encrypted is None


@pytest.mark.integration
async def test_clear_api_key_is_idempotent(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="no-key",
        model_alias="no-key",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    # No exception, no state change.
    await clear_api_key(db_session, model)
    assert model.api_key_encrypted is None


@pytest.mark.integration
async def test_create_model_rejects_generic_without_inference_endpoint(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    with pytest.raises(MissingInferenceEndpointError) as exc_info:
        await create_model(
            db_session,
            app_settings,
            name="urlless",
            model_alias="urlless",
            provider=ProviderVendor.GENERIC,
            provider_model_id="m",
        )

    assert exc_info.value.errors is not None
    assert exc_info.value.errors[0].loc == ["body", "inference_endpoint"]


@pytest.mark.integration
async def test_create_model_rejects_generic_with_blank_inference_endpoint(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # Whitespace is not a URL — it would reach `api_base` as-is and fail at the provider.
    with pytest.raises(MissingInferenceEndpointError):
        await create_model(
            db_session,
            app_settings,
            name="blank",
            model_alias="blank",
            provider=ProviderVendor.GENERIC,
            provider_model_id="m",
            inference_endpoint="   ",
        )


@pytest.mark.integration
@pytest.mark.parametrize("provider", [ProviderVendor.HUGGINGFACE, ProviderVendor.AZURE])
async def test_create_model_allows_hosted_provider_without_inference_endpoint(
    db_session: AsyncSession, app_settings: Settings, provider: ProviderVendor
) -> None:
    # HF serverless routes through the HF router and Azure can take its base from
    # the environment — only `generic` is left with nowhere to dispatch.
    model = await create_model(
        db_session,
        app_settings,
        name=f"hosted-{provider.value}",
        model_alias=f"hosted-{provider.value}",
        provider=provider,
        provider_model_id="m",
    )

    assert model.inference_endpoint is None


@pytest.mark.integration
async def test_update_model_rejects_switch_to_generic_without_endpoint(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="switcher",
        model_alias="switcher",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    with pytest.raises(MissingInferenceEndpointError):
        await update_model(db_session, model, AiModelUpdateChanges.model_validate({"provider": "generic"}))


@pytest.mark.integration
async def test_update_model_rejects_clearing_endpoint_on_generic_row(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="clearer",
        model_alias="clearer",
        provider=ProviderVendor.GENERIC,
        provider_model_id="m",
        inference_endpoint="http://slm:8080/v1",
    )

    with pytest.raises(MissingInferenceEndpointError):
        await update_model(db_session, model, AiModelUpdateChanges.model_validate({"inference_endpoint": None}))


@pytest.mark.integration
async def test_update_model_accepts_switch_to_generic_with_endpoint(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # Both halves of the pair in one patch — the guard's passing arm.
    model = await create_model(
        db_session,
        app_settings,
        name="promoter",
        model_alias="promoter",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    updated = await update_model(
        db_session,
        model,
        AiModelUpdateChanges.model_validate({"provider": "generic", "inference_endpoint": "http://slm:8080/v1"}),
    )

    assert updated.provider is ProviderVendor.GENERIC
    assert updated.inference_endpoint == "http://slm:8080/v1"


@pytest.mark.integration
async def test_update_model_leaves_legacy_generic_row_editable(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # Rows predating the rule must stay editable — the guard only fires on a patch
    # that touches `provider` or `inference_endpoint`.
    model = AiModel(
        name="legacy",
        model_alias="legacy",
        provider=ProviderVendor.GENERIC,
        provider_model_id="m",
    )
    db_session.add(model)
    await db_session.flush()

    updated = await update_model(db_session, model, AiModelUpdateChanges.model_validate({"name": "legacy renamed"}))

    assert updated.name == "legacy renamed"
    assert updated.inference_endpoint is None


@pytest.mark.integration
async def test_soft_delete_model_stamps_deleted_at(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="goodbye",
        model_alias="goodbye",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    await soft_delete_model(db_session, model, by_id=uuid4())

    assert model.deleted_at is not None
    refreshed = AiModel.live_select()
    result = await db_session.execute(refreshed)
    assert model.id not in {row.id for row in result.scalars().all()}


@pytest.mark.integration
async def test_create_model_defaults_labels_to_empty(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="unlabelled",
        model_alias="unlabelled",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
    )

    assert model.labels == []


@pytest.mark.integration
async def test_list_labels_returns_the_distinct_set_sorted(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="one",
        model_alias="one",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        labels=["self-hosted", "eval-only"],
    )
    await create_model(
        db_session,
        app_settings,
        name="two",
        model_alias="two",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        labels=["self-hosted", "Audited", "Zebra"],
    )

    # `Zebra` before `apple`-less peers only under a case-insensitive key; a plain `sorted()`
    # would hoist both capitals above `eval-only`.
    assert await list_labels(db_session) == ["Audited", "eval-only", "self-hosted", "Zebra"]


@pytest.mark.integration
async def test_list_labels_counts_disabled_models(db_session: AsyncSession, app_settings: Settings) -> None:
    await create_model(
        db_session,
        app_settings,
        name="parked",
        model_alias="parked",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        labels=["retired-fleet"],
        is_disabled=True,
    )

    assert await list_labels(db_session) == ["retired-fleet"]


@pytest.mark.integration
async def test_list_labels_skips_soft_deleted_models(db_session: AsyncSession, app_settings: Settings) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="gone",
        model_alias="gone",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4",
        labels=["only-here"],
    )

    await soft_delete_model(db_session, model, by_id=uuid4())

    assert await list_labels(db_session) == []


@pytest.mark.integration
async def test_update_model_clears_the_mismatch_when_the_declaration_changes(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # The finding accused a specific declaration; once that changes it is stale, and
    # stale here reads as current.
    model = await create_model(
        db_session,
        app_settings,
        name="Mismatch",
        model_alias="mismatch-svc",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )
    model.capability_mismatch = "unsupported content type: image_url"
    await db_session.flush()

    await update_model(db_session, model, AiModelUpdateChanges(input_modalities=[Modality.TEXT]))

    assert model.capability_mismatch is None


@pytest.mark.integration
async def test_update_model_keeps_the_mismatch_on_an_unrelated_edit(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    model = await create_model(
        db_session,
        app_settings,
        name="Mismatch keep",
        model_alias="mismatch-keep-svc",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-4o",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )
    model.capability_mismatch = "unsupported content type: image_url"
    await db_session.flush()

    # The payload shape the console actually sends: the edit form puts the whole body in
    # every PATCH, `input_modalities` included, so a test patching `name` alone would pass
    # against a clear keyed on the field's presence — the bug this asserts against.
    await update_model(
        db_session,
        model,
        AiModelUpdateChanges(name="Renamed", input_modalities=[Modality.TEXT, Modality.IMAGE]),
    )

    assert model.capability_mismatch is not None


@pytest.mark.integration
async def test_update_model_clears_the_mismatch_when_the_provider_model_id_moves(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # `provider_model_id` decides whether the model has vision more directly than the endpoint
    # does, so a finding against the old id says nothing about the new one.
    model = await create_model(
        db_session,
        app_settings,
        name="Mismatch id",
        model_alias="mismatch-id-svc",
        provider=ProviderVendor.OPENAI,
        provider_model_id="gpt-3.5-turbo",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )
    model.capability_mismatch = "unsupported content type: image_url"
    await db_session.flush()

    await update_model(db_session, model, AiModelUpdateChanges(provider_model_id="gpt-4o"))

    assert model.capability_mismatch is None


@pytest.mark.integration
async def test_update_model_clears_the_mismatch_when_the_endpoint_moves(
    db_session: AsyncSession, app_settings: Settings
) -> None:
    # The endpoint half of the same rule — seeded, so this fails if the clause is dropped.
    model = await create_model(
        db_session,
        app_settings,
        name="Mismatch endpoint",
        model_alias="mismatch-endpoint-svc",
        provider=ProviderVendor.GENERIC,
        provider_model_id="m",
        inference_endpoint="http://old:8080/v1",
        input_modalities=[Modality.TEXT, Modality.IMAGE],
    )
    model.capability_mismatch = "unsupported content type: image_url"
    await db_session.flush()

    await update_model(db_session, model, AiModelUpdateChanges(inference_endpoint="http://new:8080/v1"))

    assert model.capability_mismatch is None
