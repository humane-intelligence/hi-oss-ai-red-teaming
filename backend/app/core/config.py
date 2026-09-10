"""Application settings — env-driven via pydantic-settings, accessed through ``get_settings()``."""

from functools import lru_cache
from typing import Literal

from pydantic import EmailStr
from pydantic import Field
from pydantic import SecretStr
from pydantic import model_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

# Safe at module load: catalog.py imports only the stdlib (no app modules), so there is no cycle.
from app.core.licenses.catalog import DEFAULT_DATA_LICENSE_SPDX_ID
from app.core.licenses.catalog import is_selectable_platform_default

# Seconds added to the health-check wake-deadline for the probe task's hard `time_limit`, covering
# everything that runs past the deadline: the probe in flight when it lands, the capability probe on a
# reachable endpoint (each bounded by `dispatch._PROBE_TIMEOUT`, 10s), and the settling writes. It
# doubles as the staleness horizon (see `Settings.health_check_task_time_limit_seconds`), so a killed
# task never leaves a row un-restartable — which is the other half of raising it: an operator whose
# worker was SIGKILLed waits this much longer before a re-check is accepted.
_HEALTH_CHECK_TASK_MARGIN_SECONDS = 45


class Settings(BaseSettings):
    """Process-wide configuration loaded from environment variables and ``.env``.

    Field names map directly to env var names (uppercased), e.g.
    ``database_host`` ← ``DATABASE_HOST``. Unknown env vars are ignored so
    the same ``.env`` can be shared with sibling services.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["test", "local", "dev", "prod"]

    # Deployed commit SHA — deploy.sh writes it into .env; empty locally → "dev".
    git_sha: str = ""

    database_host: str
    database_port: int = 5432
    database_user: str
    database_password: str
    database_name: str

    # Password for the read-only `monitoring` Postgres role (`pg_monitor` — stats
    # views only, no table access) used by the opt-in `postgres-exporter` compose
    # service. Unset skips role creation in `scripts.seed_local`. See
    # monitoring/README.md "Postgres role for postgres-exporter".
    postgres_monitoring_password: SecretStr | None = None

    redis_host: str
    redis_port: int = 6379

    session_jwt_secret: SecretStr = Field(..., min_length=32)
    session_jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    session_ttl_seconds: int = 86400
    refresh_token_ttl_seconds: int = 2_592_000  # 30 days
    # Absolute ceiling on a refreshed session: rotation re-mints with a fresh
    # `exp` each time, so without this a continuously-refreshed token never
    # expires. Carried as the immutable `auth_time` claim and enforced at
    # `/auth/refresh` (stateless — no token store needed). Must exceed
    # `refresh_token_ttl_seconds` (enforced at startup) or sliding never happens.
    refresh_absolute_max_lifetime_seconds: int = 7_776_000  # 90 days

    oauth_state_secret: SecretStr = Field(..., min_length=32)
    oauth_cookie_secure: bool = True
    oauth_redirect_base_url: str = Field(..., min_length=1)

    # Google OIDC client credentials. Both unset (the default) leaves Google out of
    # the provider registry entirely, so `/auth/oidc/providers` returns it only when
    # a login would actually work and a deployment without an IdP is unaffected.
    oidc_google_client_id: str | None = None
    oidc_google_client_secret: SecretStr | None = None

    # Symmetric key used to encrypt AI-model API keys at rest via JWE (`dir` + `A256GCM`). Conversation
    # message text is sealed with `conversation_secrets_key` and never with this one. Any ≥32-char value
    # works — the encrypt path derives a 32-byte key via SHA-256. New ciphertexts are tagged with this
    # key's id; rotate by promoting a fresh value here and moving the old one
    # to `model_secrets_key_retired`.
    model_secrets_key: SecretStr = Field(..., min_length=32)
    # Outgoing key during a rotation window: kept readable so ciphertexts
    # written under it still decrypt while a background job re-wraps them under
    # the active key. Unset outside of rotations; drop once re-wrapping is done.
    model_secrets_key_retired: SecretStr | None = Field(default=None, min_length=32)

    # Key for conversation message text — never the model one, so the two datasets keep separate
    # blast radii. Required: a deployment that omits it fails at boot instead of sealing transcripts
    # under the credential key.
    conversation_secrets_key: SecretStr = Field(..., min_length=32)
    conversation_secrets_key_retired: SecretStr | None = Field(default=None, min_length=32)

    # Per-provider default API keys. Used by the gateway when an `AiModel`
    # row has no `api_key_encrypted` of its own. `aws_bedrock` (IAM creds)
    # and `generic` (caller-supplied auth) intentionally have no field.
    huggingface_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    azure_api_key: SecretStr | None = None
    cohere_api_key: SecretStr | None = None

    # Credential for the opt-in self-hosted `slm` compose service (llama.cpp,
    # started by `make upllm`). The default is a throwaway dev value the local
    # server enforces (litellm's openai path only needs it non-empty); typed
    # `SecretStr` for parity with the other provider keys and redaction if ever
    # repointed at a real-keyed server. The one source shared by the compose
    # `--api-key`, the seeded `local-slm` row, and the e2e test; change it in
    # `.env` to rotate all three.
    slm_api_key: SecretStr = SecretStr("local")

    # JSON list of browser origins allowed to call the API. Credentials are on, so no "*".
    cors_origins: list[str] = Field(default_factory=list)

    # Celery broker on Redis DB /1, separate from the app cache (/0).
    # Override CELERY_BROKER_URL to point at a different broker (e.g. RabbitMQ).
    celery_broker_url: str | None = None

    # Reaper for orphaned streaming messages: a hard crash (SIGKILL/OOM) leaves an
    # assistant placeholder stuck in `streaming` (its detached finalize never ran).
    # `ttl_seconds` must comfortably exceed the longest real generation so an
    # in-flight stream is never reaped; the periodic sweep runs every
    # `interval_seconds` (Celery beat). See app/core/conversations/tasks.py.
    streaming_reap_ttl_seconds: int = 900
    streaming_reap_interval_seconds: int = 300

    # Manual per-model health check: the async probe loop retries a STARTING endpoint every
    # `backoff` seconds up to `wake_deadline` (waiting out a cold start). The abandonment horizon is
    # derived (`health_check_task_time_limit_seconds`), not a separate knob, so it can't drift below it.
    health_check_wake_deadline_seconds: int = Field(default=120, gt=0)
    health_check_backoff_seconds: int = Field(default=5, ge=0)

    # Inactivity sweep for warmup-enabled models: a scale-to-zero endpoint kept warm but
    # unused burns money, so admins are alerted once a model's own `inactivity_alert_hours`
    # of quiet pass (per-model opt-in; warmups don't count as usage). Sweep cadence only —
    # the threshold lives on the row. See app/core/ai_gateway/tasks.py.
    model_inactivity_check_interval_seconds: int = Field(default=3600, gt=0)

    # Orphan GC for the media store: a live asset older than the grace period that no
    # known consumer references (inventory in app/core/media/tasks.py) is soft-deleted
    # and its blob removed. The grace keeps a just-uploaded image alive while its owner
    # attaches it; the sweep runs on Celery beat every `interval_seconds`.
    media_orphan_grace_hours: int = Field(default=168, gt=0)
    media_orphan_reap_interval_seconds: int = Field(default=86400, gt=0)

    # Hard cap on the number of conversations a single conversation group may
    # hold. Enforced both when batch-creating a group and when adding/moving a
    # conversation into one. See app/core/conversations/.
    max_conversation_group_size: int = 4

    # How long a soft-deleted row stays restorable. Bounds both the `deleted=true`
    # listings and the restore endpoints — past it, a tombstone is a 404 (nothing
    # purges it, it just stops being addressable). See app/core/restore.py.
    restore_window_days: int = Field(default=7, gt=0)

    # Asynchronous export jobs: every export is generated in the background by a Celery
    # worker and stored for download (owner/admin action; there is no inline path).
    # `export_storage_backend` is the single switch selecting the storage adapter — each
    # provider's config coexists below and only the selected one is read. Widen the
    # `Literal` + add a branch in `get_export_storage` to add a backend. See
    # app/core/exports/storage/.
    export_storage_backend: Literal["local", "s3"] = "local"
    # local: base directory the finished export files are written into.
    export_storage_dir: str = "var/exports"
    # s3: bucket + key prefix (credentials come from the default boto3 chain — instance
    # profile in prod, AWS_* env vars locally — never from here; region reuses `aws_region`).
    export_s3_bucket: str | None = None
    export_s3_prefix: str = "exports/"
    # A finished export file is downloadable until this long after completion; the
    # reaper drops it (and the job row) afterwards. The periodic sweep runs every
    # `export_reap_interval_seconds` (Celery beat). See app/core/exports/tasks.py.
    export_job_ttl_seconds: int = 86400
    export_reap_interval_seconds: int = 3600
    # A job stuck in `pending`/`running` longer than this (worker crash, or the broker
    # dropped the message so no acks_late redelivery fires) is failed by the reaper — it
    # carries no `expires_at`, so the TTL sweep never touches it. Mirrors the streaming reaper.
    export_stuck_ttl_seconds: int = 3600

    # Generic media storage for uploaded images (covers/avatars/icons): byte blobs behind
    # a port mirroring exports. `media_storage_backend` is the single switch selecting the
    # adapter; each provider's config coexists below and only the selected one is read.
    # Widen the `Literal` + add a branch in `get_media_storage` to add a backend. See
    # app/core/media/storage/.
    media_storage_backend: Literal["local", "s3"] = "local"
    # local: base directory uploaded images are written into.
    media_storage_dir: str = "var/media"
    # s3: bucket + key prefix (credentials come from the default boto3 chain; region reuses
    # `aws_region`). The prefix is applied by the adapter, never baked into the stored key.
    media_s3_bucket: str | None = None
    media_s3_prefix: str = "images/"
    # TTL for a signed media URL (app-signed token on the proxy-GET, not S3-presigned). One
    # fixed value for now; configurable-per-use-case is a Standard-tier concern. See
    # app/core/media/signing.py.
    media_signed_url_ttl_seconds: int = Field(default=900, gt=0)
    # Dedicated signing secret for media URLs; when unset, falls back to `session_jwt_secret`.
    # Set it to rotate media-URL signing independently of session tokens (and vice versa).
    media_url_signing_secret: SecretStr | None = Field(default=None, min_length=32)

    # Default DATA license applied to evaluation/conversation data when none is set
    # explicitly — the platform default shipped with the OSS release. Distinct from
    # the code license (Apache-2.0). Overridable at runtime via the platform-settings
    # singleton (app/core/licenses/); this env value is the initial/fallback default.
    # Defaults to the catalog's designated default so the two never drift.
    platform_default_data_license: str = DEFAULT_DATA_LICENSE_SPDX_ID

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    # Stamped as `service` on every JSON log line so log queries can tell the
    # API and the Celery worker apart (both run this same codebase).
    service_name: str = "ai-red-teaming-api"

    # Error reporting is opt-in: unset (the default) makes `init_sentry` a no-op, so OSS
    # deployments without an error-reporting backend configured are unaffected. Uses the
    # Sentry SDK protocol against a self-hosted GlitchTip (see README "Error reporting").
    sentry_dsn: SecretStr | None = None

    # Email delivery backend. `console` logs only; `ses` uses AWS SESv2; `smtp`
    # uses a configured SMTP server. See app/core/email/backends/.
    email_backend: Literal["console", "ses", "smtp"] = "console"
    email_from: EmailStr
    # Optional postal / company line for the email footer. Omitted from the
    # footer entirely when unset (pending from the customer).
    email_footer_address: str | None = None
    # Branding injected into every transactional mail as the Jinja `brand` context:
    # the entity operating the platform, and the platform/product itself.
    brand_company: str = "Humane Intelligence"
    brand_product: str = "AI Red Teaming"

    # AWS region for the `ses` backend; boto3 takes credentials from its default chain.
    aws_region: str = "us-east-1"

    # SMTP backend connection (used only when email_backend == "smtp"). `smtp_tls`:
    # "none" (e.g. local Mailpit), "starttls" (port 587), "ssl" (implicit TLS, port 465).
    # Auth is skipped when smtp_username is unset.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_tls: Literal["none", "starttls", "ssl"] = "starttls"

    # SPA base URL embedded in transactional mails. No trailing slash.
    frontend_base_url: str = Field(..., min_length=1)
    invitation_ttl_hours: int = 168
    # Seeds the platform-settings knob (transient reads + first materialization) — keep the env
    # value inside the PATCH contract's bounds, or a bad deploy mints out-of-contract links.
    email_verification_ttl_hours: int = Field(default=24, ge=1, le=8760)
    # Password-reset links are incident-response, not onboarding — keep TTL short.
    password_reset_ttl_hours: int = 24

    # Bulk endpoint hard cap. Requests above this fail validation; large
    # imports should be queued through Celery instead. See
    # app/core/bulk.py.
    bulk_max_rows: int = 1000

    # Connection pool — tune via env vars for production workloads.
    # pool_pre_ping is always on (see database.py) and not exposed here.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800  # recycle connections after 30 min to avoid stale state
    db_echo: bool = False  # set DB_ECHO=true to log all SQL (development only)

    @model_validator(mode="after")
    def _validate_model_secret_keyring(self) -> Settings:
        """Fail at startup if an active and retired secret key collide on key-id.

        Building each keyring surfaces the (negligible but possible) id collision as a config error at
        boot, rather than as a runtime decrypt failure on the first request. Model credentials and
        message text are separate mechanisms with separate keys, so each is checked by its own module.
        Imported lazily to avoid a config↔crypto import cycle.
        """
        if self.model_secrets_key_retired is not None:
            from app.core.ai_gateway.crypto import validate_keyring  # noqa: PLC0415 — lazy: config↔crypto cycle

            validate_keyring(self)
        if self.conversation_secrets_key_retired is not None:
            from app.core.conversations.content_crypto import validate_content_keyring  # noqa: PLC0415 — as above

            validate_content_keyring(self)
        return self

    @model_validator(mode="after")
    def _validate_secret_key_separation(self) -> Settings:
        """Fail at startup if the transcript keyring shares a value with the model-credential one.

        Sealing transcripts under a credential secret is not a key-id collision, so the per-keyring
        check above cannot see it — and the two would then rotate and leak together, which is the one
        property the separate key exists for. Retired slots count: a value still able to open one
        dataset must not open the other.
        """
        model = {self.model_secrets_key.get_secret_value()}
        if self.model_secrets_key_retired is not None:
            model.add(self.model_secrets_key_retired.get_secret_value())
        conversation = {self.conversation_secrets_key.get_secret_value()}
        if self.conversation_secrets_key_retired is not None:
            conversation.add(self.conversation_secrets_key_retired.get_secret_value())
        if model & conversation:
            msg = (
                "CONVERSATION_SECRETS_KEY (or its retired slot) shares a value with MODEL_SECRETS_KEY; "
                "transcripts and model credentials must not share key material."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_export_storage(self) -> Settings:
        """Fail at startup if the selected export storage backend is missing required config.

        So `export_storage_backend=s3` without a bucket errors at boot, not on the first
        export job (which would land `failed` for every requester until noticed).
        """
        if self.export_storage_backend == "s3" and not self.export_s3_bucket:
            raise ValueError("export_s3_bucket must be set when export_storage_backend='s3'.")
        return self

    @model_validator(mode="after")
    def _validate_media_storage(self) -> Settings:
        """Fail at startup if the selected media storage backend is missing required config.

        So `media_storage_backend=s3` without a bucket errors at boot, not on the first upload.
        """
        if self.media_storage_backend == "s3" and not self.media_s3_bucket:
            raise ValueError("media_s3_bucket must be set when media_storage_backend='s3'.")
        return self

    @model_validator(mode="after")
    def _validate_default_data_license(self) -> Settings:
        """Fail at startup unless the configured platform default is a selectable catalog licence.

        Surfaces a typo'd `PLATFORM_DEFAULT_DATA_LICENSE` — or the no-license sentinel, which would
        unlicense every inheriting group — as a boot error rather than as a bogus
        `effective_license` on every inheriting evaluation.
        """
        if not is_selectable_platform_default(self.platform_default_data_license):
            raise ValueError(
                f"platform_default_data_license '{self.platform_default_data_license}' is not a selectable "
                "platform default; see app/core/licenses/catalog.py for valid SPDX ids."
            )
        return self

    @model_validator(mode="after")
    def _validate_refresh_lifetime(self) -> Settings:
        """Reject a config where the absolute cap can't exceed a single token's TTL.

        If `refresh_absolute_max_lifetime_seconds` is not strictly greater than
        `refresh_token_ttl_seconds`, the absolute ceiling rejects a refresh token
        at or before its own `exp`, so sliding-window rotation silently never
        happens. Fail at startup rather than degrade to fixed-lifetime sessions.
        """
        if self.refresh_absolute_max_lifetime_seconds <= self.refresh_token_ttl_seconds:
            raise ValueError(
                "refresh_absolute_max_lifetime_seconds "
                f"({self.refresh_absolute_max_lifetime_seconds}) must exceed "
                f"refresh_token_ttl_seconds ({self.refresh_token_ttl_seconds}); "
                "otherwise the absolute cap rejects refresh tokens before their "
                "own exp and sliding never happens."
            )
        return self

    @model_validator(mode="after")
    def _validate_oidc_credentials(self) -> Settings:
        """Reject a half-configured OIDC provider at startup, and a blank-looking one.

        A client id without its secret (or vice versa) would silently drop the provider
        from the registry — the login button just disappears, with nothing in the logs
        pointing at the typo'd env var. `.env.example` ships both as an empty string for
        "leave blank to disable Google login" — that must keep working (`bool("")` is
        `False`, same as `None`), so only a *non-empty* value that's still blank after
        stripping (a stray copy-pasted space) is rejected as broken rather than unset.
        """
        client_id = self.oidc_google_client_id
        client_secret = self.oidc_google_client_secret
        if client_id and not client_id.strip():
            raise ValueError("oidc_google_client_id must not be whitespace-only.")
        if client_secret and not client_secret.get_secret_value().strip():
            raise ValueError("oidc_google_client_secret must not be whitespace-only.")
        if bool(client_id) != bool(client_secret):
            raise ValueError(
                "oidc_google_client_id and oidc_google_client_secret must be set together; "
                "one without the other silently disables Google login."
            )
        return self

    @model_validator(mode="after")
    def _validate_email_backend(self) -> Settings:
        """Fail at startup on an incoherent email config, not mid-task in the worker.

        Without these a missing `smtp_host` (or a username with no password)
        surfaces only when the Celery worker sends the first mail.
        """
        if self.email_backend == "smtp":
            if not self.smtp_host:
                raise ValueError("smtp_host is required when email_backend='smtp'")
            if self.smtp_username and self.smtp_password is None:
                raise ValueError("smtp_password is required when smtp_username is set")
        return self

    @property
    def database_url(self) -> str:
        """Async SQLAlchemy URL built from the discrete ``database_*`` fields."""
        return (
            f"postgresql+asyncpg://{self.database_user}:{self.database_password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @property
    def database_url_sync(self) -> str:
        """Sync DSN used by Celery tasks via the psycopg (v3) driver."""
        return (
            f"postgresql+psycopg://{self.database_user}:{self.database_password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @property
    def redis_url(self) -> str:
        """Redis connection URL targeting database index 0."""
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or f"redis://{self.redis_host}:{self.redis_port}/1"

    @property
    def health_check_task_time_limit_seconds(self) -> int:
        """Hard `time_limit` for the health-check Celery task, and the row's staleness horizon.

        Once this elapses the task is guaranteed dead (SIGKILL at the limit), so a `checking` row
        older than it is abandoned and a new check may override it — deriving both from one value
        removes the window a separate `stale_ttl > time_limit` left, where a killed task still blocked
        a restart. An override at the boundary is safe: the CAS on the start stamp drops the loser.
        """
        return self.health_check_wake_deadline_seconds + _HEALTH_CHECK_TASK_MARGIN_SECONDS


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide `Settings` singleton.

    Cached with `functools.lru_cache` so env vars and ``.env`` are read once
    per process. Tests that need to override settings should clear the cache
    or override the FastAPI dependency, not mutate the returned instance.

    Returns:
        The shared `Settings` instance for this process.
    """
    return Settings()
