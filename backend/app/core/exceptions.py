"""Domain-level API exceptions.

Routers raise these instead of `fastapi.HTTPException`. The handlers in
`app.core.error_handlers` translate them into RFC 7807 `Problem` responses
with `Content-Type: application/problem+json`.
"""

from fastapi import status

from app.core.schemas import ProblemErrorItem


class APIError(Exception):
    """Base class for errors that translate into an RFC 7807 response.

    Subclasses set `status_code`, `title`, and `type` as class attributes;
    callers supply `detail` per occurrence. An optional `errors` list carries
    field-level detail (RFC 7807 `errors[]`, the same shape a 422 uses) so a
    client can map the error onto a form field.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    title: str = "Internal Server Error"
    type: str = "about:blank"
    errors: list[ProblemErrorItem] | None = None

    def __init__(self, detail: str | None = None) -> None:
        """Store the occurrence-specific detail; fall back to ``title`` for ``str(exc)``.

        Args:
            detail: Human-readable explanation of this specific occurrence.
                Stored on ``self.detail`` and surfaced in the Problem
                response body. If omitted, the class-level ``title`` is
                used as the exception's ``args[0]`` so logs and reprs are
                still meaningful.
        """
        super().__init__(detail or self.title)
        self.detail = detail


class BadRequestError(APIError):
    """Client supplied a syntactically valid but semantically invalid request — 400."""

    status_code = status.HTTP_400_BAD_REQUEST
    title = "Bad Request"


class UnauthorizedError(APIError):
    """Request is missing or has invalid authentication credentials — 401."""

    status_code = status.HTTP_401_UNAUTHORIZED
    title = "Unauthorized"


class ForbiddenError(APIError):
    """Caller is authenticated but not permitted to perform this action — 403."""

    status_code = status.HTTP_403_FORBIDDEN
    title = "Forbidden"


class NotFoundError(APIError):
    """Target resource does not exist — 404."""

    status_code = status.HTTP_404_NOT_FOUND
    title = "Not Found"


class ConflictError(APIError):
    """Request conflicts with current resource state (duplicate, version skew) — 409."""

    status_code = status.HTTP_409_CONFLICT
    title = "Conflict"


class GoneError(APIError):
    """Resource existed but is permanently unavailable (accepted, revoked, expired) — 410."""

    status_code = status.HTTP_410_GONE
    title = "Gone"


class BadGatewayError(APIError):
    status_code = status.HTTP_502_BAD_GATEWAY
    title = "Bad Gateway"


class ServiceUnavailableError(APIError):
    """A dependency the request needs is temporarily unavailable (e.g. the task broker) — 503."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    title = "Service Unavailable"
