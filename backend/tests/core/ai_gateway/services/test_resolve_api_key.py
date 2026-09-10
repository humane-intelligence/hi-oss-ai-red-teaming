"""Unit tests for `resolve_api_key` — model-key vs env-var precedence."""

from typing import Any

import pytest
from pydantic import SecretStr

from app.core.ai_gateway.crypto import encrypt_secret
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.models import AiModel
from app.core.ai_gateway.services.ai_models import _OUT_OF_BAND_CREDENTIAL_VENDORS
from app.core.ai_gateway.services.ai_models import _PROVIDER_DEFAULT_KEY_FIELD
from app.core.ai_gateway.services.ai_models import resolve_api_key
from app.core.config import Settings


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "model_secrets_key": SecretStr("test-model-secrets-key-at-least-32-chars"),
    }
    return Settings.model_construct(**(base | overrides))


def _model(provider: ProviderVendor, *, api_key_encrypted: str | None = None) -> AiModel:
    return AiModel(
        name="x",
        model_alias="x",
        provider=provider,
        provider_model_id="x",
        api_key_encrypted=api_key_encrypted,
    )


@pytest.mark.unit
def test_model_level_key_wins_over_env_default() -> None:
    settings = _settings(openai_api_key=SecretStr("env-key"))
    encrypted = encrypt_secret("row-key", settings)
    model = _model(ProviderVendor.OPENAI, api_key_encrypted=encrypted)

    resolved = resolve_api_key(model, settings)

    assert resolved is not None
    assert resolved.get_secret_value() == "row-key"


@pytest.mark.unit
def test_falls_back_to_env_when_row_has_no_key() -> None:
    settings = _settings(anthropic_api_key=SecretStr("env-anthropic"))
    model = _model(ProviderVendor.ANTHROPIC)

    resolved = resolve_api_key(model, settings)

    assert resolved is not None
    assert resolved.get_secret_value() == "env-anthropic"


@pytest.mark.unit
def test_returns_none_when_row_and_env_both_absent() -> None:
    settings = _settings()
    model = _model(ProviderVendor.OPENAI)

    assert resolve_api_key(model, settings) is None


@pytest.mark.unit
@pytest.mark.parametrize("provider", [ProviderVendor.AWS_BEDROCK, ProviderVendor.GENERIC])
def test_providers_without_env_field_return_none(provider: ProviderVendor) -> None:
    """`aws_bedrock` and `generic` intentionally have no env-var fallback."""
    model = _model(provider)

    assert resolve_api_key(model, _settings()) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider", "field"),
    [
        (ProviderVendor.HUGGINGFACE, "huggingface_api_key"),
        (ProviderVendor.GOOGLE, "google_api_key"),
        (ProviderVendor.OPENAI, "openai_api_key"),
        (ProviderVendor.ANTHROPIC, "anthropic_api_key"),
        (ProviderVendor.AZURE, "azure_api_key"),
        (ProviderVendor.COHERE, "cohere_api_key"),
    ],
)
def test_each_provider_reads_its_own_settings_field(provider: ProviderVendor, field: str) -> None:
    settings = _settings(**{field: SecretStr(f"key-for-{provider.value}")})
    model = _model(provider)

    resolved = resolve_api_key(model, settings)

    assert resolved is not None
    assert resolved.get_secret_value() == f"key-for-{provider.value}"


@pytest.mark.unit
def test_default_key_fields_exist_on_settings() -> None:
    # Guards the "keep in sync" comment on _PROVIDER_DEFAULT_KEY_FIELD: a typo'd
    # field name would AttributeError at dispatch time, not here.
    missing = [field for field in _PROVIDER_DEFAULT_KEY_FIELD.values() if field not in Settings.model_fields]
    assert not missing


@pytest.mark.unit
def test_every_vendor_declares_which_credential_side_it_is_on() -> None:
    # A vendor in neither collection inherits `generic`'s answer from `has_resolvable_credential`
    # ("no credential"), which silently stops the post-write health check for its endpoint-less rows.
    assert set(ProviderVendor) == _PROVIDER_DEFAULT_KEY_FIELD.keys() | _OUT_OF_BAND_CREDENTIAL_VENDORS | {
        ProviderVendor.GENERIC
    }
    # Equal-union alone allows a vendor in both: `has_resolvable_credential` answers off the
    # frozenset, `resolve_api_key` hands out the `Settings` key, and the two disagree about one row.
    assert not _PROVIDER_DEFAULT_KEY_FIELD.keys() & _OUT_OF_BAND_CREDENTIAL_VENDORS
