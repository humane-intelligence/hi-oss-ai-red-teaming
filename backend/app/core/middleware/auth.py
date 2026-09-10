"""Bearer-token validation middleware — populates `request.state.user`."""

from collections.abc import Awaitable
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.auth.services.jwt import InvalidSessionTokenError
from app.core.auth.services.jwt import decode_session_jwt
from app.core.auth.services.session_revocation import is_revoked
from app.core.config import get_settings


class AuthMiddleware(BaseHTTPMiddleware):
    """Decode a bearer JWT (if present) and attach `SessionUser` to `request.state.user`.

    Missing / invalid / expired tokens leave `request.state.user = None` — the
    request continues as anonymous and individual route dependencies decide
    whether to require an authenticated user.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Decode the bearer token (if any) and forward the request.

        Sets ``request.state.user`` to a `SessionUser` for a valid bearer
        token and to ``None`` otherwise — including when the header is
        missing, the scheme is not ``Bearer``, or the token fails
        verification. The request is always forwarded; per-route
        dependencies decide whether to require a populated user.

        Args:
            request: Incoming Starlette request.
            call_next: Downstream ASGI callable to forward the request to.

        Returns:
            The downstream response, unmodified.
        """
        request.state.user = None

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() == "bearer" and token:
            settings = get_settings()
            try:
                user = decode_session_jwt(
                    token,
                    secret=settings.session_jwt_secret.get_secret_value(),
                    algorithm=settings.session_jwt_algorithm,
                )
            except InvalidSessionTokenError:
                user = None
            # A force-logout revokes every session minted before its marker;
            # drop the identity to anonymous so downstream deps see no user.
            if user is not None and await is_revoked(user.id, user.issued_at):
                user = None
            request.state.user = user

        return await call_next(request)
