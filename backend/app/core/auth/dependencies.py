"""Auth-scoped route dependencies — callables and `Annotated` aliases.

Cross-cutting wrappers live in `app/core/dependencies.py`.
"""

from collections.abc import Callable
from typing import Annotated
from typing import Final

from fastapi import Depends
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.filters import UserFilters
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.database import get_db
from app.core.exceptions import ForbiddenError
from app.core.exceptions import UnauthorizedError
from app.core.terms.service import TermsAcceptanceRequiredError
from app.core.terms.service import acceptance_required_for

UserFiltersDep = Annotated[UserFilters, Depends()]


# Endpoints that answer while an acceptance is owed, or an account could never clear it: it has to
# learn that it owes one, read the document, and record the acceptance. Hand-pinned rather than
# derived, on the same reasoning as `_PUBLIC_ALLOWLIST` in the endpoint-guard tests — every entry
# is a hole in the enforcement, so adding one belongs in a diff somebody reviews.
#
# Keyed on the endpoint's name, in the same spirit as the audit middleware (which keys on the
# `(method, name)` pair): a matched route's `path` is relative to the router that declared it
# (`/me`, not `/api/v1/auth/me`), so it identifies nothing on its own. The name alone is enough
# here only because a sweep pins each one to the single method and full path it may resolve to.
#
# `GET /terms/current` needs no entry: it is public, so it never resolves this dependency.
CONSENT_EXEMPT_ENDPOINTS: Final[frozenset[str]] = frozenset(
    {
        "me",
        "accept_terms_endpoint",
        "get_terms_endpoint",
    }
)


async def current_user(
    request: Request,
    # `DbSession` lives in `app/core/dependencies.py`, which imports this module — hence the raw
    # `Depends(get_db)` rather than the alias routes are told to use.
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SessionUser:
    """Admit an authenticated caller: identity from `AuthMiddleware`, then the consent gate.

    `AuthMiddleware` sets ``request.state.user`` to a `SessionUser` for valid bearer tokens and to
    ``None`` otherwise; the anonymous case becomes a 401, so routes depending on this can assume a
    populated session user.

    Consent is enforced **here** rather than on the router, because this is the one place every
    authenticated route already passes through: a route added later is covered without anybody
    remembering to cover it, and the console's own gate is a route guard for the same reason.
    Placing it on the router instead would also gate the public routes for any caller who happens
    to carry a token — refusing a token-bearing `POST /auth/refresh` to the very account sitting on
    the acceptance screen. Routes taking `optional_current_user` are not gated here: they serve
    anonymous traffic, so identity is a bonus rather than the thing being authorised — except in
    the mint/fetch pair for a *private* image, whose signed URL is bound to the caller: those two
    branches authorise on identity and so gate themselves in `app/api/v1/images.py`.

    Raises:
        UnauthorizedError: If no user is attached to the request.
        TermsAcceptanceRequiredError: If a version is published and this account has not accepted
            it, unless the endpoint is one of `CONSENT_EXEMPT_ENDPOINTS`.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise UnauthorizedError("not authenticated")
    if getattr(request.scope.get("route"), "name", None) in CONSENT_EXEMPT_ENDPOINTS:
        return user
    if await acceptance_required_for(db, user.id):
        raise TermsAcceptanceRequiredError
    return user


def optional_current_user(request: Request) -> SessionUser | None:
    """Resolve the session user like `current_user`, but yield None for anonymous callers.

    For routes serving both anonymous and authenticated traffic that only branch on
    identity when one is present (e.g. per-user signed media URLs).

    Carries no consent gate — a branch that authorises on the identity this returns has to
    apply one itself.
    """
    return getattr(request.state, "user", None)


def require_permission(permission: Permission) -> Callable[..., SessionUser]:
    """Build a route dependency that gates access on ``permission``.

    The factory returns a callable that FastAPI resolves like any other
    dependency. Use it from a route as:

        user: Annotated[SessionUser, Depends(require_permission(Permission.USERS_READ))]

    Args:
        permission: Permission key the caller must hold. Checked against
            `SessionUser.permissions`, which is materialised from the JWT
            claim at token-mint time.

    Returns:
        A dependency callable yielding the authenticated `SessionUser`.

    Raises:
        ForbiddenError: When the resolved user does not hold ``permission``.
    """

    def _check(user: Annotated[SessionUser, Depends(current_user)]) -> SessionUser:
        if permission not in user.permissions:
            raise ForbiddenError(f"Caller lacks the '{permission}' permission.")
        return user

    return _check
