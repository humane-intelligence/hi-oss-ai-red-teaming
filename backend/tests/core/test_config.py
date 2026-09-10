"""Tests for Settings / get_settings — env-driven config accessor."""

from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from pydantic import ValidationError

from app.core.config import Settings
from app.core.config import get_settings


@pytest.fixture
def _env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_HOST", "db.example.com")
    monkeypatch.setenv("DATABASE_PORT", "5433")
    monkeypatch.setenv("DATABASE_USER", "alice")
    monkeypatch.setenv("DATABASE_PASSWORD", "s3cret")
    monkeypatch.setenv("DATABASE_NAME", "mydb")
    monkeypatch.setenv("REDIS_HOST", "redis.example.com")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("SESSION_JWT_SECRET", "test-secret-comfortably-long-for-hs256")


@pytest.fixture
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.unit
def test_database_url_builds_asyncpg_dsn() -> None:
    settings = Settings.model_construct(
        database_host="db.example.com",
        database_port=5433,
        database_user="alice",
        database_password="s3cret",
        database_name="mydb",
        redis_host="redis.example.com",
        redis_port=6380,
    )

    assert settings.database_url == "postgresql+asyncpg://alice:s3cret@db.example.com:5433/mydb"


@pytest.mark.unit
def test_redis_url_builds_dsn_with_db_zero() -> None:
    settings = Settings.model_construct(
        database_host="localhost",
        database_port=5432,
        database_user="user",
        database_password="pass",
        database_name="db",
        redis_host="redis.example.com",
        redis_port=6380,
    )

    assert settings.redis_url == "redis://redis.example.com:6380/0"


@pytest.mark.unit
def test_database_url_sync_builds_psycopg_dsn() -> None:
    settings = Settings.model_construct(
        database_host="db.example.com",
        database_port=5433,
        database_user="alice",
        database_password="s3cret",
        database_name="mydb",
        redis_host="localhost",
        redis_port=6379,
        celery_broker_url=None,
    )

    assert settings.database_url_sync == "postgresql+psycopg://alice:s3cret@db.example.com:5433/mydb"


@pytest.mark.unit
def test_broker_url_defaults_to_redis_db_one() -> None:
    settings = Settings.model_construct(
        database_host="localhost",
        database_port=5432,
        database_user="user",
        database_password="pass",
        database_name="db",
        redis_host="redis.example.com",
        redis_port=6380,
        celery_broker_url=None,
    )

    assert settings.broker_url == "redis://redis.example.com:6380/1"


@pytest.mark.unit
def test_broker_url_respects_env_override() -> None:
    settings = Settings.model_construct(
        database_host="localhost",
        database_port=5432,
        database_user="user",
        database_password="pass",
        database_name="db",
        redis_host="redis.example.com",
        redis_port=6380,
        celery_broker_url="amqp://rabbit:5672//",
    )

    assert settings.broker_url == "amqp://rabbit:5672//"


@pytest.mark.unit
@pytest.mark.usefixtures("_env_vars", "_clear_settings_cache")
def test_get_settings_loads_from_environment() -> None:
    settings = get_settings()

    assert settings.database_host == "db.example.com"
    assert settings.database_port == 5433
    assert settings.database_user == "alice"
    assert settings.database_password == "s3cret"
    assert settings.database_name == "mydb"
    assert settings.redis_host == "redis.example.com"
    assert settings.redis_port == 6380


@pytest.mark.unit
@pytest.mark.usefixtures("_clear_settings_cache")
def test_get_settings_uses_default_ports_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_USER", "user")
    monkeypatch.setenv("DATABASE_PASSWORD", "pass")
    monkeypatch.setenv("DATABASE_NAME", "db")
    monkeypatch.setenv("REDIS_HOST", "localhost")
    monkeypatch.setenv("SESSION_JWT_SECRET", "test-secret-comfortably-long-for-hs256")
    monkeypatch.delenv("DATABASE_PORT", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)

    settings = get_settings()

    assert settings.database_port == 5432
    assert settings.redis_port == 6379


@pytest.mark.unit
def test_colliding_model_secret_keys_fail_settings_validation() -> None:
    """A real `Settings(...)` build rejects active/retired keys sharing a key-id.

    The crypto unit tests use `Settings.model_construct(...)`, which bypasses
    validators, so the startup `_validate_model_secret_keyring` check is only
    exercised here. Identical keys are the guaranteed key-id collision.
    """
    key = SecretStr("shared-model-secrets-key-at-least-32-chars")
    with pytest.raises(ValidationError, match="collide on key-id"):
        Settings(
            environment="test",
            database_host="localhost",
            database_user="user",
            database_password="pass",
            database_name="db",
            redis_host="localhost",
            session_jwt_secret=SecretStr("test-secret-comfortably-long-for-hs256"),
            oauth_state_secret=SecretStr("oauth-state-secret-comfortably-long-256"),
            oauth_redirect_base_url="http://localhost:8000",
            model_secrets_key=key,
            model_secrets_key_retired=key,
            email_from="noreply@example.com",
            frontend_base_url="http://localhost:3000",
        )


@pytest.mark.unit
@pytest.mark.parametrize("retired", [False, True])
def test_a_transcript_key_borrowed_from_the_model_keyring_fails_validation(retired: bool) -> None:
    """Startup refuses key material shared between the two datasets.

    Nothing else catches it: sharing a secret is not a key-id collision inside either keyring, and the
    formats differ, so both mechanisms keep working while the separate blast radii they are documented
    to have are gone. A retired slot counts — it still opens rows.
    """
    shared = SecretStr("shared-between-both-keyrings-32-chars-plus")
    unrelated = SecretStr("model-secrets-key-comfortably-long-32ch")
    with pytest.raises(ValidationError, match="must not share key material"):
        Settings(
            environment="test",
            database_host="localhost",
            database_user="user",
            database_password="pass",
            database_name="db",
            redis_host="localhost",
            session_jwt_secret=SecretStr("test-secret-comfortably-long-for-hs256"),
            oauth_state_secret=SecretStr("oauth-state-secret-comfortably-long-256"),
            oauth_redirect_base_url="http://localhost:8000",
            email_from="noreply@example.com",
            frontend_base_url="http://localhost:3000",
            model_secrets_key=unrelated if retired else shared,
            model_secrets_key_retired=shared if retired else None,
            conversation_secrets_key=shared,
        )


@pytest.mark.unit
def test_refresh_absolute_cap_below_ttl_fails_settings_validation() -> None:
    """A real `Settings(...)` build rejects an absolute cap that can't exceed the token TTL.

    When the cap is not strictly greater than the per-token TTL the absolute
    ceiling rejects a refresh token at or before its own `exp`, so sliding never
    happens — caught at startup rather than degrading to fixed-lifetime sessions.
    """
    with pytest.raises(ValidationError, match="must exceed"):
        Settings(
            environment="test",
            database_host="localhost",
            database_user="user",
            database_password="pass",
            database_name="db",
            redis_host="localhost",
            session_jwt_secret=SecretStr("test-secret-comfortably-long-for-hs256"),
            oauth_state_secret=SecretStr("oauth-state-secret-comfortably-long-256"),
            oauth_redirect_base_url="http://localhost:8000",
            model_secrets_key=SecretStr("model-secrets-key-at-least-32-chars-long"),
            email_from="noreply@example.com",
            frontend_base_url="http://localhost:3000",
            refresh_token_ttl_seconds=2_592_000,
            refresh_absolute_max_lifetime_seconds=2_592_000,
        )


@pytest.mark.unit
def test_smtp_backend_without_host_fails_settings_validation() -> None:
    """`EMAIL_BACKEND=smtp` without a host is rejected at startup, not mid-send in the worker."""
    with pytest.raises(ValidationError, match="smtp_host is required"):
        Settings(
            environment="test",
            database_host="localhost",
            database_user="user",
            database_password="pass",
            database_name="db",
            redis_host="localhost",
            session_jwt_secret=SecretStr("test-secret-comfortably-long-for-hs256"),
            oauth_state_secret=SecretStr("oauth-state-secret-comfortably-long-256"),
            oauth_redirect_base_url="http://localhost:8000",
            model_secrets_key=SecretStr("model-secrets-key-at-least-32-chars-long"),
            email_from="noreply@example.com",
            frontend_base_url="http://localhost:3000",
            email_backend="smtp",
        )


def _settings_with_oidc(client_id: str | None, client_secret: SecretStr | str | None) -> Settings:
    return Settings(
        environment="test",
        database_host="localhost",
        database_user="user",
        database_password="pass",
        database_name="db",
        redis_host="localhost",
        session_jwt_secret=SecretStr("test-secret-comfortably-long-for-hs256"),
        oauth_state_secret=SecretStr("oauth-state-secret-comfortably-long-256"),
        oauth_redirect_base_url="http://localhost:8000",
        model_secrets_key=SecretStr("model-secrets-key-at-least-32-chars-long"),
        email_from="noreply@example.com",
        frontend_base_url="http://localhost:3000",
        oidc_google_client_id=client_id,
        oidc_google_client_secret=client_secret,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [("google-client-id", None), (None, SecretStr("google-client-secret"))],
)
def test_half_configured_oidc_provider_fails_settings_validation(
    client_id: str | None,
    client_secret: SecretStr | None,
) -> None:
    """One OIDC credential without the other is rejected at startup.

    Otherwise the provider silently drops out of the registry — the login button
    disappears with nothing pointing at the typo'd env var.
    """
    with pytest.raises(ValidationError, match="must be set together"):
        _settings_with_oidc(client_id, client_secret)


@pytest.mark.unit
def test_both_oidc_google_credentials_blank_disables_google_login() -> None:
    """`.env.example` documents an empty string on both as "leave blank to disable
    Google login" — that must keep working, not crash the app at boot.
    """
    settings = _settings_with_oidc("", "")

    assert settings.oidc_google_client_id == ""
    assert settings.oidc_google_client_secret is not None
    assert settings.oidc_google_client_secret.get_secret_value() == ""


@pytest.mark.unit
def test_whitespace_only_oidc_google_client_id_fails_settings_validation() -> None:
    """Unlike a genuinely blank value, a stray copy-pasted space is a broken credential
    that would otherwise pass the both-or-neither check right alongside a real secret
    and get advertised as a working provider.
    """
    with pytest.raises(ValidationError, match="whitespace-only"):
        _settings_with_oidc(" ", SecretStr("google-secret"))


@pytest.mark.unit
def test_smtp_username_without_password_fails_settings_validation() -> None:
    """A username with no password is a silent misconfig — caught at startup."""
    with pytest.raises(ValidationError, match="smtp_password is required"):
        Settings(
            environment="test",
            database_host="localhost",
            database_user="user",
            database_password="pass",
            database_name="db",
            redis_host="localhost",
            session_jwt_secret=SecretStr("test-secret-comfortably-long-for-hs256"),
            oauth_state_secret=SecretStr("oauth-state-secret-comfortably-long-256"),
            oauth_redirect_base_url="http://localhost:8000",
            model_secrets_key=SecretStr("model-secrets-key-at-least-32-chars-long"),
            email_from="noreply@example.com",
            frontend_base_url="http://localhost:3000",
            email_backend="smtp",
            smtp_host="mail.example.com",
            smtp_username="user",
        )


@pytest.mark.unit
def test_smtp_username_without_password_ignored_when_backend_not_smtp() -> None:
    """SMTP creds are inert under console/ses — a stray username must not block boot."""
    settings = Settings(
        environment="test",
        database_host="localhost",
        database_user="user",
        database_password="pass",
        database_name="db",
        redis_host="localhost",
        session_jwt_secret=SecretStr("test-secret-comfortably-long-for-hs256"),
        oauth_state_secret=SecretStr("oauth-state-secret-comfortably-long-256"),
        oauth_redirect_base_url="http://localhost:8000",
        model_secrets_key=SecretStr("model-secrets-key-at-least-32-chars-long"),
        email_from="noreply@example.com",
        frontend_base_url="http://localhost:3000",
        email_backend="console",
        smtp_username="user",
    )

    assert settings.email_backend == "console"


@pytest.mark.unit
def test_media_storage_s3_without_bucket_fails_settings_validation() -> None:
    """`MEDIA_STORAGE_BACKEND=s3` without a bucket is rejected at startup, not on the first upload."""
    with pytest.raises(ValidationError, match="media_s3_bucket must be set"):
        Settings(
            environment="test",
            database_host="localhost",
            database_user="user",
            database_password="pass",
            database_name="db",
            redis_host="localhost",
            session_jwt_secret=SecretStr("test-secret-comfortably-long-for-hs256"),
            oauth_state_secret=SecretStr("oauth-state-secret-comfortably-long-256"),
            oauth_redirect_base_url="http://localhost:8000",
            model_secrets_key=SecretStr("model-secrets-key-at-least-32-chars-long"),
            email_from="noreply@example.com",
            frontend_base_url="http://localhost:3000",
            media_storage_backend="s3",
        )


@pytest.mark.unit
def test_media_signed_url_ttl_non_positive_fails_settings_validation() -> None:
    """A TTL <= 0 makes every minted media URL dead-on-arrival (fetch always 403) — rejected at startup."""
    with pytest.raises(ValidationError, match="greater than 0"):
        Settings(
            environment="test",
            database_host="localhost",
            database_user="user",
            database_password="pass",
            database_name="db",
            redis_host="localhost",
            session_jwt_secret=SecretStr("test-secret-comfortably-long-for-hs256"),
            oauth_state_secret=SecretStr("oauth-state-secret-comfortably-long-256"),
            oauth_redirect_base_url="http://localhost:8000",
            model_secrets_key=SecretStr("model-secrets-key-at-least-32-chars-long"),
            email_from="noreply@example.com",
            frontend_base_url="http://localhost:3000",
            media_signed_url_ttl_seconds=0,
        )


@pytest.mark.unit
@pytest.mark.parametrize("bad_ttl", [0, 8761])
def test_email_verification_ttl_outside_the_patch_bounds_fails_settings_validation(bad_ttl: int) -> None:
    """The env value seeds the admin knob, so a deploy outside the PATCH contract's 1-8760 must not boot."""
    with pytest.raises(ValidationError, match="email_verification_ttl_hours"):
        Settings(
            environment="test",
            database_host="localhost",
            database_user="user",
            database_password="pass",
            database_name="db",
            redis_host="localhost",
            session_jwt_secret=SecretStr("test-secret-comfortably-long-for-hs256"),
            oauth_state_secret=SecretStr("oauth-state-secret-comfortably-long-256"),
            oauth_redirect_base_url="http://localhost:8000",
            model_secrets_key=SecretStr("model-secrets-key-at-least-32-chars-long"),
            email_from="noreply@example.com",
            frontend_base_url="http://localhost:3000",
            email_verification_ttl_hours=bad_ttl,
        )


@pytest.mark.unit
@pytest.mark.usefixtures("_env_vars", "_clear_settings_cache")
def test_get_settings_returns_cached_instance() -> None:
    first = get_settings()
    second = get_settings()

    assert first is second


@pytest.mark.unit
def test_no_license_sentinel_as_platform_default_fails_settings_validation() -> None:
    """The no-license sentinel is refused as `PLATFORM_DEFAULT_DATA_LICENSE`.

    It would otherwise boot cleanly and leave the transient settings row pointing at the
    sentinel, unlicensing everything that inherits, with no DB row to inspect.
    """
    with pytest.raises(ValidationError, match="not a selectable platform default"):
        Settings(
            environment="test",
            database_host="localhost",
            database_user="user",
            database_password="pass",
            database_name="db",
            redis_host="localhost",
            session_jwt_secret=SecretStr("test-secret-comfortably-long-for-hs256"),
            oauth_state_secret=SecretStr("oauth-state-secret-comfortably-long-256"),
            oauth_redirect_base_url="http://localhost:8000",
            model_secrets_key=SecretStr("model-secrets-key-at-least-32-chars-long"),
            email_from="noreply@example.com",
            frontend_base_url="http://localhost:3000",
            platform_default_data_license="NONE",
        )
