"""OIDC provider registry — one dispatch surface for all configured IdPs.

Credentials come from `Settings` (env), so a provider is registered only when its
client id + secret are configured. TODO: move the provider list itself to a
database-backed registry once schemas land; new IdPs land in `_configured_specs`
for now.
"""

from dataclasses import dataclass
from functools import lru_cache

from authlib.integrations.starlette_client import OAuth
from authlib.integrations.starlette_client import StarletteOAuth2App
from pydantic import SecretStr

from app.core.config import get_settings
from app.core.exceptions import NotFoundError

_GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"


@dataclass(frozen=True)
class _ProviderSpec:
    name: str
    client_id: str
    client_secret: SecretStr
    server_metadata_url: str
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    # When False, an id_token with `email_verified=false` is rejected at login.
    # Flip to True only for IdPs whose verification is out-of-band trustworthy.
    trust_email_unverified: bool = False


def _configured_specs() -> tuple[_ProviderSpec, ...]:
    """Specs for every IdP whose credentials are present in the environment."""
    settings = get_settings()
    if not (settings.oidc_google_client_id and settings.oidc_google_client_secret):
        return ()
    return (
        _ProviderSpec(
            name="google",
            client_id=settings.oidc_google_client_id,
            client_secret=settings.oidc_google_client_secret,
            server_metadata_url=_GOOGLE_METADATA_URL,
        ),
    )


def _build_oauth() -> OAuth:
    oauth = OAuth()
    for spec in _configured_specs():
        oauth.register(
            name=spec.name,
            client_id=spec.client_id,
            client_secret=spec.client_secret.get_secret_value(),
            server_metadata_url=spec.server_metadata_url,
            client_kwargs={
                "scope": " ".join(spec.scopes),
                # PKCE: defends against auth-code interception on the redirect leg.
                "code_challenge_method": "S256",
            },
        )
    return oauth


@lru_cache
def get_oauth() -> OAuth:
    """Cached OAuth registry — built from the configured specs above.

    Cached because `register` is per-process setup, not per-request work; tests
    that repoint `Settings` must call `get_oauth.cache_clear()`.
    """
    return _build_oauth()


def get_provider(name: str) -> StarletteOAuth2App:
    """Resolve a configured provider by `{provider}` path-param name.

    Returns the authlib client (used to drive `authorize_redirect` /
    `authorize_access_token`). Raises 404 for unknown *and* unconfigured names so
    callers don't need to pre-validate.
    """
    client = get_oauth().create_client(name)
    if client is None:
        raise NotFoundError(f"unknown OIDC provider: {name}")
    return client


def provider_names() -> list[str]:
    """Names of every provider a login could currently be completed with."""
    return [spec.name for spec in _configured_specs()]


def provider_trusts_unverified_email(name: str) -> bool:
    """Whether the named provider's `email_verified=false` should still issue a session.

    Raises 404 for unknown provider names, mirroring `get_provider`.
    """
    for spec in _configured_specs():
        if spec.name == name:
            return spec.trust_email_unverified
    raise NotFoundError(f"unknown OIDC provider: {name}")
