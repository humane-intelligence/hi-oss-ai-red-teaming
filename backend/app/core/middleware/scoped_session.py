"""`SessionMiddleware` wrapper that only engages on configured path prefixes.

Starlette's `SessionMiddleware` is mounted app-wide by `add_middleware`, which
means every request would parse + sign the session cookie even when only the
OIDC redirect roundtrip needs `request.session`. Wrapping it lets us scope the
cookie strictly to the OIDC routes, so requests to `/health`, `/api/v1/...`
etc. never touch the session machinery.
"""

from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import MutableMapping
from typing import Any

from starlette.middleware.sessions import SessionMiddleware

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class ScopedSessionMiddleware:
    """Delegate to `SessionMiddleware` only when the request path matches a prefix."""

    def __init__(self, app: ASGIApp, *, prefixes: tuple[str, ...], **session_kwargs: Any) -> None:
        """Wrap `app` and pre-build the underlying `SessionMiddleware`.

        Args:
            app: Downstream ASGI callable.
            prefixes: Path prefixes that opt into session handling. A path
                matches when it equals a prefix exactly or extends it with
                `/...` — `/api/v1/auth/oidc` and `/api/v1/auth/oidc/google`
                match the prefix `/api/v1/auth/oidc`, but `/api/v1/auth/oidcx`
                does not.
            **session_kwargs: Forwarded verbatim to `SessionMiddleware`
                (`secret_key`, `session_cookie`, `max_age`, ...).
        """
        self._app = app
        self._session_app = SessionMiddleware(app, **session_kwargs)
        self._prefixes = prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._matches(scope["path"]):
            await self._session_app(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _matches(self, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in self._prefixes)
