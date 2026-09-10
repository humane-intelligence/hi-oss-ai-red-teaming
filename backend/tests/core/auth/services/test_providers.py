"""OIDC provider registry — specs built from configured credentials."""

from collections.abc import Iterator

import pytest

from app.core.auth.services import providers as providers_mod
from app.core.exceptions import NotFoundError
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def _reset_oauth_cache() -> Iterator[None]:
    providers_mod.get_oauth.cache_clear()
    yield
    # Also on teardown: a test that repoints Settings would otherwise leave the
    # registry it built behind for whatever module runs next.
    providers_mod.get_oauth.cache_clear()


@pytest.fixture
def unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repoint the registry at a Settings carrying no OIDC credentials."""
    settings = make_settings(oidc_google_client_id=None, oidc_google_client_secret=None)
    monkeypatch.setattr(providers_mod, "get_settings", lambda: settings)


@pytest.mark.unit
def test_provider_names_lists_configured_providers() -> None:
    assert providers_mod.provider_names() == ["google"]


@pytest.mark.unit
def test_get_provider_returns_client_for_known_name() -> None:
    client = providers_mod.get_provider("google")

    assert client is not None
    assert client.name == "google"


@pytest.mark.unit
def test_get_provider_raises_not_found_for_unknown_name() -> None:
    with pytest.raises(NotFoundError):
        providers_mod.get_provider("nonexistent")


@pytest.mark.unit
def test_provider_trusts_unverified_email_defaults_to_false() -> None:
    assert providers_mod.provider_trusts_unverified_email("google") is False


@pytest.mark.unit
def test_provider_trusts_unverified_email_raises_not_found_for_unknown_name() -> None:
    with pytest.raises(NotFoundError):
        providers_mod.provider_trusts_unverified_email("nonexistent")


@pytest.mark.unit
def test_oauth_registers_clients_with_pkce_s256() -> None:
    client = providers_mod.get_provider("google")

    assert client.client_kwargs.get("code_challenge_method") == "S256"


@pytest.mark.unit
def test_provider_uses_credentials_from_settings() -> None:
    client = providers_mod.get_provider("google")

    assert client.client_id == "test-google-client-id"


@pytest.mark.unit
@pytest.mark.usefixtures("unconfigured")
def test_provider_names_is_empty_without_credentials() -> None:
    assert providers_mod.provider_names() == []


@pytest.mark.unit
@pytest.mark.usefixtures("unconfigured")
def test_get_provider_raises_not_found_without_credentials() -> None:
    with pytest.raises(NotFoundError):
        providers_mod.get_provider("google")


@pytest.mark.unit
@pytest.mark.usefixtures("unconfigured")
def test_provider_trusts_unverified_email_raises_not_found_without_credentials() -> None:
    with pytest.raises(NotFoundError):
        providers_mod.provider_trusts_unverified_email("google")
