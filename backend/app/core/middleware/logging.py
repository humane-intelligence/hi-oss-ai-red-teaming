"""ASGI middleware that prepares per-request context and emits an access log.

`configure_logging` already includes `structlog.contextvars.merge_contextvars`
in the processor chain; this middleware populates those contextvars so every
log line emitted during a request — including the access log emitted here —
carries `request_id`, `method`, and `path`.
"""

import time
import uuid
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import MutableMapping
from typing import Any

import sentry_sdk
import structlog

from app.core.logging import get_logger

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

REQUEST_ID_HEADER = b"x-request-id"
ACCESS_LOG_EVENT = "http_request"
_UNHANDLED_EXCEPTION_STATUS = 500

access_logger = get_logger(__name__)


class LoggingMiddleware:
    """Bind per-request context and emit one access log line per request.

    Generates a fresh `uuid4().hex` per request, binds `request_id` / `method`
    / `path` into `structlog.contextvars`, echoes the id in the `X-Request-ID`
    response header, and on completion emits an `http_request` event with
    `status_code` and `duration_ms`.

    Inbound `X-Request-ID` is intentionally ignored: until a trusted upstream
    sets it, the value would be attacker-controlled.

    If the inner app raises before sending a response, the access log fires
    with `status_code=500` so unhandled exceptions still produce a record.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Store the wrapped ASGI app.

        Args:
            app: Downstream ASGI callable this middleware delegates to.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Wrap one ASGI request: bind log context, stamp ``X-Request-ID``, emit access log.

        Lifecycle types other than ``http`` (websocket, lifespan) are
        forwarded untouched. For HTTP requests, a fresh ``uuid4().hex`` is
        bound into ``structlog.contextvars`` so every log line emitted
        downstream — including the access log written here — carries
        ``request_id`` / ``method`` / ``path``. The same id is appended to
        the response headers as ``X-Request-ID``.

        The access log is emitted in a ``finally`` block so unhandled
        exceptions still produce a record (with ``status_code=500``).
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        status_code = _UNHANDLED_EXCEPTION_STATUS

        # Set on the isolation scope, which survives the contextvars unwind below before
        # the outermost layer captures an unhandled 500. Harmless when Sentry is off — the
        # tag lands on a scope no client reads.
        sentry_sdk.get_isolation_scope().set_tag("request_id", request_id)

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER, request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        start = time.perf_counter()
        with structlog.contextvars.bound_contextvars(
            request_id=request_id,
            method=scope["method"],
            path=scope["path"],
        ):
            try:
                await self.app(scope, receive, send_with_request_id)
            finally:
                access_logger.info(
                    ACCESS_LOG_EVENT,
                    status_code=status_code,
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                )
