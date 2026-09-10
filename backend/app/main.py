"""FastAPI application entrypoint — builds the ``app`` object imported by uvicorn."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.health import router as health_router
from app.api.v1 import v1_router
from app.api.version import router as version_router
from app.core import database as db
from app.core.auth.services import session_revocation
from app.core.config import get_settings
from app.core.error_handlers import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import AuditAccessMiddleware
from app.core.middleware import LoggingMiddleware
from app.core.middleware import ScopedSessionMiddleware
from app.core.middleware.auth import AuthMiddleware
from app.core.openapi import APP_DESCRIPTION
from app.core.openapi import APP_SUMMARY
from app.core.openapi import APP_TITLE
from app.core.openapi import CONTACT
from app.core.openapi import LICENSE_INFO
from app.core.openapi import OPENAPI_TAGS
from app.core.openapi import app_version
from app.core.sentry import init_sentry

# Registers the redis-backed Celery app as this process's current_app, so `@shared_task` producers resolve to it.
from app.workers.celery_app import app as _celery_app  # noqa: F401


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Build process-wide singletons on startup, tear them down on shutdown.

    Configures logging first so any engine-build failure is captured, then
    initialises the async SQLAlchemy engine. The engine is always disposed,
    even if startup fails mid-way.
    """
    configure_logging(get_settings())
    await db.init_engine(get_settings())
    await session_revocation.init_client(get_settings())
    try:
        yield
    finally:
        await session_revocation.close_client()
        await db.dispose_engine()


# Configure logging, then init Sentry — both before FastAPI is built. Sentry must
# init before the middleware stack (see init_sentry); a no-op without SENTRY_DSN.
configure_logging(get_settings())
init_sentry(get_settings())

app = FastAPI(
    title=APP_TITLE,
    version=app_version(),
    summary=APP_SUMMARY,
    description=APP_DESCRIPTION,
    contact=CONTACT,
    license_info=LICENSE_INFO,
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

settings = get_settings()

# `request.session` is only consumed by authlib during the OIDC redirect
# roundtrip (state + nonce), so the underlying `SessionMiddleware` is scoped
# to the OIDC paths via `ScopedSessionMiddleware` — every other request skips
# the cookie parse entirely. The secret is required by config so workers in a
# multi-process deploy share it; without that, a login redirect that lands on
# a different worker than it started on would fail state validation.
# Added first ⇒ innermost: wraps the router directly (sees the matched route +
# path params) and runs inside AuthMiddleware (sees request.state.user). Audits
# allowlisted read/access routes after the response, in its own session.
app.add_middleware(AuditAccessMiddleware)
app.add_middleware(
    ScopedSessionMiddleware,
    prefixes=("/api/v1/auth/oidc",),
    secret_key=settings.oauth_state_secret.get_secret_value(),
    session_cookie="oidc_state",
    max_age=600,
    same_site="lax",
    https_only=settings.oauth_cookie_secure,
)
app.add_middleware(LoggingMiddleware)
app.add_middleware(AuthMiddleware)

# Added last so CORS sits outermost — it answers preflight before auth and wraps
# every response. Origins are explicit (never "*") because credentials are allowed.
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

register_error_handlers(app)

app.include_router(health_router)
app.include_router(version_router)
app.include_router(v1_router)

# HTTP RED metrics + process/GC defaults on GET /metrics. The endpoint is
# unauthenticated but never routed through the reverse proxy — Prometheus
# scrapes it over the compose network. Probes are excluded as scrape noise.
Instrumentator(excluded_handlers=["^/health$", "^/ready$", "^/metrics$"]).instrument(app).expose(
    app, include_in_schema=False
)
