"""Sentry error reporting — opt-in via `SENTRY_DSN`; a no-op when unset."""

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import ignore_logger
from sentry_sdk.integrations.starlette import StarletteIntegration

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.openapi import display_version

logger = get_logger(__name__)

# Our RFC 7807 BadGatewayError/ServiceUnavailableError (502/503 — flaky target-model
# endpoints) are expected operational noise, not incidents; only report hard 500s.
_REPORTED_STATUS_CODES = {500}


def init_sentry(settings: Settings) -> None:
    """Initialize Sentry error reporting; a no-op when `settings.sentry_dsn` is unset.

    Errors only (`traces_sample_rate=0.0` — no performance tracing) and no default PII.
    FastAPI/Starlette report only plain 500s (`_REPORTED_STATUS_CODES`); the Celery
    integration auto-enables (task exceptions reported) whenever `celery` is importable.
    `request_id` is attached to events by `LoggingMiddleware` (`method`/`path` come from
    the SDK's own request capture).

    Must run at import time, before the FastAPI app is built: the Starlette/FastAPI
    integrations attach their `failed_request_status_codes` filter by patching the
    middleware stack at init, and the stack is assembled lazily on the first (lifespan)
    ASGI scope — after a lifespan-time init would have run, leaving the filter inert.
    `max_request_body_size="never"` keeps request bodies (red-team prompts) out of
    events (`send_default_pii=False` does not gate the body). `include_local_variables=False`
    because neither of those flags touches per-frame local variables: a traceback crossing
    asyncpg/SQLAlchemy internals (e.g. a DB-down connection error) both blows past
    GlitchTip's envelope size limit (silently dropped — the SDK doesn't surface ingest
    errors without `debug=True`) and risks shipping secrets that happen to be in scope
    (asyncpg's own `_connect_addr` holds the DB password as a local, for one).

    `sentry_sdk.init` raises synchronously on a malformed DSN (`BadDsn`, a `ValueError`)
    — caught here so a typo'd `SENTRY_DSN` degrades to "no error reporting" instead of
    crashing the process at import.
    """
    dsn = settings.sentry_dsn.get_secret_value() if settings.sentry_dsn is not None else ""
    if not dsn:
        return
    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=settings.environment,
            release=display_version(settings.git_sha),
            send_default_pii=False,
            max_request_body_size="never",
            include_local_variables=False,
            traces_sample_rate=0.0,
            # No outbound trace headers: litellm calls reach externally-registered,
            # untrusted target-model endpoints — don't leak sentry-trace/baggage there.
            trace_propagation_targets=[],
            integrations=[
                StarletteIntegration(failed_request_status_codes=_REPORTED_STATUS_CODES),
                FastApiIntegration(failed_request_status_codes=_REPORTED_STATUS_CODES),
            ],
        )
        # The catch-all handler re-logs every unhandled exception the Starlette
        # integration already captures; without this each 500 is reported twice
        # (DedupeIntegration can't collapse them — its ContextVar doesn't survive the
        # threadpool/middleware boundary between the two capture points).
        ignore_logger("app.core.error_handlers")
    except Exception:
        logger.exception("sentry_init_failed")
