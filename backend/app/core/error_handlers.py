"""FastAPI exception handlers — translate exceptions into RFC 7807 responses.

Wire by calling `register_error_handlers(app)` once from `app/main.py`. All
handlers return `ProblemResponse`, which sets `application/problem+json`.
"""

from collections.abc import Awaitable
from collections.abc import Callable
from http import HTTPStatus

from fastapi import FastAPI
from fastapi import Request
from fastapi import status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import APIError
from app.core.logging import get_logger
from app.core.schemas import Problem
from app.core.schemas import ProblemErrorItem

logger = get_logger(__name__)


class ProblemResponse(JSONResponse):
    """JSONResponse subclass that sets the RFC 7807 media type."""

    media_type = "application/problem+json"


def api_error_to_problem(exc: APIError, *, instance: str | None) -> Problem:
    """Map an `APIError` to its RFC 7807 `Problem` envelope.

    Used by the global handler and by bulk-operation helpers that inline a
    `Problem` per failed row (`app.core.bulk`). Keeping a single mapper
    guarantees the same shape regardless of where the error surfaces.

    Args:
        exc: The domain error to render.
        instance: URI reference identifying the occurrence — typically the
            request path. Pass ``None`` for synthetic contexts (e.g. inline
            per-row errors in a bulk response, where the parent request is
            the relevant instance).
    """
    return Problem(
        type=exc.type,
        title=exc.title,
        status=exc.status_code,
        detail=exc.detail,
        instance=instance,
        errors=exc.errors,
    )


async def _handle_api_error(request: Request, exc: APIError) -> ProblemResponse:
    """Render a domain `APIError` as RFC 7807, adding ``WWW-Authenticate`` on 401."""
    problem = api_error_to_problem(exc, instance=str(request.url.path))
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == status.HTTP_401_UNAUTHORIZED else None
    return ProblemResponse(
        status_code=exc.status_code,
        content=problem.model_dump(exclude_none=True),
        headers=headers,
    )


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> ProblemResponse:
    """Render a Pydantic validation failure as 422 with field-level ``errors[]``."""
    errors = [ProblemErrorItem(loc=list(err["loc"]), msg=err["msg"], type=err["type"]) for err in exc.errors()]
    problem = Problem(
        title="Unprocessable Entity",
        status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="Request body failed validation.",
        instance=str(request.url.path),
        errors=errors,
    )
    return ProblemResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=problem.model_dump(exclude_none=True),
    )


async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> ProblemResponse:
    """Render a raw `HTTPException` (Starlette or FastAPI) as RFC 7807.

    Preserves the original headers — notably ``WWW-Authenticate`` on 401 and
    ``Retry-After`` on 429 / 503 — so transport-level semantics survive the
    rewrite.
    """
    title = _http_status_title(exc.status_code)
    problem = Problem(
        title=title,
        status=exc.status_code,
        detail=str(exc.detail) if exc.detail else None,
        instance=str(request.url.path),
    )
    return ProblemResponse(
        status_code=exc.status_code,
        content=problem.model_dump(exclude_none=True),
        headers=exc.headers,
    )


async def _handle_unexpected_exception(request: Request, exc: Exception) -> ProblemResponse:
    """Fallback for un-typed exceptions — log with traceback, return a sanitized 500.

    The original exception is logged with ``exc_info`` so the stack trace
    reaches the log pipeline, but the response body never leaks internals —
    only the generic ``"An unexpected error occurred."`` message.
    """
    # exc_info=exc rather than logger.exception(): Starlette invokes this from its own
    # `except` block, so relying on ambient sys.exc_info() would also trip LOG004.
    logger.error("Unhandled exception while serving %s %s", request.method, request.url.path, exc_info=exc)
    problem = Problem(
        title="Internal Server Error",
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An unexpected error occurred.",
        instance=str(request.url.path),
    )
    return ProblemResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=problem.model_dump(exclude_none=True),
    )


def _http_status_title(code: int) -> str:
    """Return the standard HTTP reason phrase for ``code``, falling back to ``"HTTP Error"``."""
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "HTTP Error"


# FastAPI's `add_exception_handler` signature is too narrow for handlers typed
# on concrete `APIError` / `RequestValidationError` / `StarletteHTTPException`
# subclasses. The ignore is localized here so adding more handlers doesn't
# scatter `ty: ignore` across the file.
def _register(
    app: FastAPI,
    exc_class: type[Exception],
    handler: Callable[..., Awaitable[ProblemResponse]],
) -> None:
    """Localize the FastAPI handler-signature type ignore to a single call site."""
    app.add_exception_handler(exc_class, handler)


def register_error_handlers(app: FastAPI) -> None:
    """Wire every Problem-producing handler onto ``app``.

    Call once at startup. Registers handlers for `APIError`,
    `RequestValidationError`, `StarletteHTTPException`, and the catch-all
    `Exception` — together they guarantee every error response leaves the
    app as RFC 7807 with ``application/problem+json``.

    Args:
        app: The FastAPI instance to attach handlers to.
    """
    _register(app, APIError, _handle_api_error)
    _register(app, RequestValidationError, _handle_validation_error)
    _register(app, StarletteHTTPException, _handle_http_exception)
    _register(app, Exception, _handle_unexpected_exception)
