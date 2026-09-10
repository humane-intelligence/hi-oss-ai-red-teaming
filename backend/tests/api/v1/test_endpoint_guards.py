"""Auto-discovering auth sweeps over every API route.

The route table is enumerated at collection time (`app.openapi()`), so a new
endpoint is swept the moment it exists, with no test change:

* every route outside the explicit `_PUBLIC_ALLOWLIST` → 401 without
  credentials. The allowlist is deliberately hand-pinned: a route that forgot
  auth entirely must be acknowledged here in the diff, never silently
  reclassified. A consistency test asserts the allowlist equals the public set
  derived from the dependency tree, so the two views cannot drift.
* every permission-gated route → a permissionless caller is refused (403; 404
  when an object-scoped gate resolves the target before checking), and a caller
  holding every permission BUT the required ones is refused too (specificity).

**Sweep boundary:** the permission map comes from closure introspection
(`scripts.openapi_permissions`), so gates that read their permission as a module
global (`require_group_read`, the metrics deps) are invisible here — those
routes keep an explicit per-route wiring test (e.g. the members-list 403), as do
all object-authority matrices (owner/manager/visibility) and conditional gates
on query flags.

**Known limits (verified, no live gap today):**

* *Enumeration asymmetry* — the 401 sweep, the consent sweep and the
  403-declaration invariant all enumerate from `_AUTHENTICATED`, i.e. from
  `app.openapi()` (schema-visible routes only), while the permission sweeps, the
  public-set derivation and the optional-auth derivation enumerate from route
  introspection. An
  `include_in_schema=False` *authenticated* route would therefore escape all
  three of the first group; only `/metrics` is hidden today, and it is public.
* *Body-before-auth* — the 401 sweep sends no request body and asserts exactly
  401, which holds only while `current_user` resolves before request-body
  validation; a route authenticating outside that dependency would 422 first.
* *Permission identity* — the specificity sweep derives the required permission
  from the same introspection it asserts on, so it proves *a* permission is
  enforced but cannot pin the permission's identity or catch dropping one of
  several required permissions; the CI-gated `permissions.md` drift dump pins
  route→permission identity.
"""

import re
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends
from fastapi import status
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.dependencies import optional_current_user
from app.core.auth.roles import Permission
from app.core.auth.schemas import SessionUser
from app.core.dependencies import OptionalUserDep
from app.core.terms.service import TermsAcceptanceRequiredError
from app.core.terms.service import publish_terms
from app.main import app
from scripts.openapi_permissions import effective_routes
from scripts.openapi_permissions import introspect_routes
from tests.api.v1.conftest import bearer
from tests.api.v1.conftest import caller_with

pytestmark = pytest.mark.integration

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

# Routes that take no bearer token, by design. Adding a new public route
# requires an entry here — the consistency test below rejects anything else.
_PUBLIC_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/version"),
        # Deliberately unauthenticated: never proxied publicly, scraped over the compose network.
        ("GET", "/metrics"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/refresh"),
        ("POST", "/api/v1/auth/register"),
        ("POST", "/api/v1/auth/register/resend"),
        ("POST", "/api/v1/auth/register/verify"),
        ("GET", "/api/v1/auth/oidc/providers"),
        ("GET", "/api/v1/auth/oidc/{provider}/login"),
        ("GET", "/api/v1/auth/oidc/{provider}/callback"),
        ("GET", "/api/v1/auth/invitations/accept"),
        ("POST", "/api/v1/auth/invitations/accept"),
        ("POST", "/api/v1/auth/password-resets/request"),
        ("POST", "/api/v1/auth/password-resets/confirm"),
        # Anonymous-safe settings subset for the login/registration screens (signup_enabled).
        ("GET", "/api/v1/platform-settings/public"),
        # The registration screen has to show the terms before an account exists, and legal text
        # the platform asks the public to accept is public by nature.
        ("GET", "/api/v1/terms/current"),
        # Image serving + signed-URL mint/fetch are public by design — the UUID key
        # (or the signed token) is the capability, and minting grants no more than
        # the already-public key does (see app/api/v1/images.py).
        ("GET", "/api/v1/images/{key}"),
        ("GET", "/api/v1/images/signed-url"),
        ("GET", "/api/v1/images/signed/{token}"),
    }
)


def _normalize(path: str) -> str:
    """Strip path-converter suffixes (`{key:path}` → `{key}`) to match OpenAPI keys."""
    return re.sub(r"{([^}:]+):[^}]+}", r"{\1}", path)


_raw_permissions, _raw_public = introspect_routes()
_PERMISSION_MAP = {(method, _normalize(path)): keys for (method, path), keys in _raw_permissions.items()}
_PUBLIC_DERIVED = {(method, _normalize(path)) for method, path in _raw_public}

_ALL = {
    (method.upper(), path)
    for path, operations in app.openapi()["paths"].items()
    for method in operations
    if method.upper() in _METHODS
}

_AUTHENTICATED = sorted(_ALL - _PUBLIC_ALLOWLIST)
_GATED = sorted(_PERMISSION_MAP)


# What `CONSENT_EXEMPT_ENDPOINTS` is allowed to resolve to. The app keys the exemption on endpoint
# names because a matched route's `path` is router-relative; this pins the other half, so a rename,
# a moved route, or a name that starts covering something else cannot pass silently.
_CONSENT_EXEMPT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/auth/me/terms"),
        ("GET", "/api/v1/terms/{terms_id}"),
    }
)


# Routes that authorise on an optional identity. `current_user` carries the consent gate, so these
# never reach it: each one that authorises on the identity it gets has to gate itself, and there is
# no dependency doing it for them. Pinned by hand for the same reason as `_PUBLIC_ALLOWLIST` —
# adding one is adding a place the enforcement can be forgotten.
_OPTIONAL_AUTH_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/images/signed-url"),
        ("GET", "/api/v1/images/signed/{token}"),
    }
)


def _reaches_optional_user(dependant: Dependant) -> bool:
    """True if `dependant` reaches `optional_current_user` at any depth.

    Recursive for the same reason `_depends_on_current_user` is (`scripts/openapi_permissions.py`):
    a resolver taking `OptionalUserDep` puts it at depth 2, and a route reaching it that way skips
    the gate exactly like a direct one. Reading only the direct dependencies would leave that route
    out of the derived set, so the sweep below would stay green while the hole shipped.
    """
    return any(
        dependency.call is optional_current_user or _reaches_optional_user(dependency)
        for dependency in dependant.dependencies
    )


def _optional_auth_derived() -> set[tuple[str, str]]:
    """`(METHOD, path)` for every route reaching `optional_current_user`, at any depth."""
    found: set[tuple[str, str]] = set()
    for path, route in effective_routes():
        if not _reaches_optional_user(route.dependant):
            continue
        found |= {(method.upper(), _normalize(path)) for method in route.methods or set() if method.upper() in _METHODS}
    return found


def _fill(path: str) -> str:
    """Substitute every path parameter with a fresh UUID string.

    Valid for UUID-typed params and inert for plain-string ones; the sweeps
    assert on gate behaviour that fires before any handler could 404 on it.
    """
    return re.sub(r"{[^}]+}", lambda _: str(uuid4()), path)


def _route_id(route: tuple[str, str]) -> str:
    return f"{route[0]} {route[1]}"


def test_public_allowlist_matches_dependency_tree() -> None:
    # Both directions: a stale allowlist entry (route gone or now authenticated)
    # and a new route that never resolves `current_user` (forgot auth?) fail here.
    assert _PUBLIC_ALLOWLIST == _PUBLIC_DERIVED, {
        "stale allowlist entries": sorted(_PUBLIC_ALLOWLIST - _PUBLIC_DERIVED),
        "unauthenticated routes missing an explicit allowlist entry": sorted(_PUBLIC_DERIVED - _PUBLIC_ALLOWLIST),
    }


def test_sweep_covers_expected_route_floor() -> None:
    # A partial `introspect_routes()` (or a collapsed openapi) would shrink the
    # parametrized sets and still pass green — the consistency test above only
    # catches a total break. Bump these when routes are legitimately removed.
    assert len(_AUTHENTICATED) >= 110, len(_AUTHENTICATED)
    assert len(_GATED) >= 100, len(_GATED)


@pytest.mark.parametrize("route", _AUTHENTICATED, ids=_route_id)
async def test_route_requires_authentication(auth_db_client: AsyncClient, route: tuple[str, str]) -> None:
    method, path = route

    response = await auth_db_client.request(method, _fill(path))

    assert response.status_code == status.HTTP_401_UNAUTHORIZED, (
        f"{method} {path} returned {response.status_code} without auth; expected 401. "
        f"Either gate the route or add it to _PUBLIC_ALLOWLIST in {__file__}."
    )
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("route", _GATED, ids=_route_id)
async def test_route_refuses_permissionless_caller(
    auth_db_client: AsyncClient, db_session: AsyncSession, route: tuple[str, str]
) -> None:
    method, path = route
    caller = await caller_with(db_session)

    response = await auth_db_client.request(method, _fill(path), headers=bearer(caller))

    # 404 is an object-scoped gate resolving the (random-UUID) target first —
    # still a refusal; the exact 403-vs-404 contract lives in the wiring tests.
    assert response.status_code in {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND}


@pytest.mark.parametrize("route", _GATED, ids=_route_id)
async def test_route_requires_its_specific_permission(
    auth_db_client: AsyncClient, db_session: AsyncSession, route: tuple[str, str]
) -> None:
    method, path = route
    required = set(_PERMISSION_MAP[route])
    almost_all = [p for p in Permission if p.value not in required]
    caller = await caller_with(db_session, *almost_all)

    response = await auth_db_client.request(method, _fill(path), headers=bearer(caller))

    # For plain `require_permission` gates this proves the exact permission is
    # required. Object-scoped gates 404 on the random-UUID target before the
    # permission arm runs (and `almost_all` includes the manage break-glass), so
    # for that class this asserts refusal only — their specificity lives in the
    # per-route wiring tests.
    assert response.status_code in {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND}


def test_consent_exemptions_are_all_authenticated_routes() -> None:
    # An exemption on a public route would be dead weight (the gate only runs where
    # `current_user` does) and would hide a real hole behind a name that looks covered.
    assert set(_AUTHENTICATED) >= _CONSENT_EXEMPT_ROUTES, sorted(_CONSENT_EXEMPT_ROUTES - set(_AUTHENTICATED))


def test_optional_auth_check_sees_a_nested_dependency() -> None:
    # The sweep below is only as good as this: a resolver taking `OptionalUserDep` puts the
    # dependency at depth 2, and a route reaching it that way skips the consent gate exactly like a
    # direct one. The second assert is the point — the direct-only form this replaced saw nothing
    # here, so such a route stayed out of the pinned set while the hole shipped.
    def resolver(caller: OptionalUserDep) -> SessionUser | None:
        return caller

    async def endpoint(_who: Annotated[SessionUser | None, Depends(resolver)]) -> None:
        return None

    nested = APIRoute("/probe", endpoint, methods=["GET"])

    assert _reaches_optional_user(nested.dependant)
    assert not any(dependency.call is optional_current_user for dependency in nested.dependant.dependencies)


def test_optional_auth_routes_are_the_pinned_set() -> None:
    # A new one bypasses the consent gate silently, so it has to arrive in a diff somebody reads.
    assert _optional_auth_derived() == set(_OPTIONAL_AUTH_ROUTES), {
        "stale entries": sorted(set(_OPTIONAL_AUTH_ROUTES) - _optional_auth_derived()),
        "ungated additions": sorted(_optional_auth_derived() - set(_OPTIONAL_AUTH_ROUTES)),
    }


def test_every_refusable_route_declares_the_403_it_can_return() -> None:
    # The behavioural sweep below proves the refusal happens; this proves the committed contract
    # admits it. A route added without a 403 in `responses=` would answer one the spec denies —
    # and the FE's generated client would have no branch for it.
    declared = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method, operation in operations.items()
        if method.upper() in _METHODS and "403" in operation.get("responses", {})
    }
    refusable = set(_AUTHENTICATED) - _CONSENT_EXEMPT_ROUTES

    assert refusable <= declared, sorted(refusable - declared)


@pytest.mark.parametrize("route", _AUTHENTICATED, ids=_route_id)
async def test_route_refuses_a_caller_who_owes_a_terms_acceptance(
    auth_db_client: AsyncClient, db_session: AsyncSession, route: tuple[str, str]
) -> None:
    method, path = route
    # Every permission, so nothing but consent can be the reason for the refusal.
    caller = await caller_with(db_session, *Permission)
    await publish_terms(db_session, version="1.0", content="Terms")

    response = await auth_db_client.request(method, _fill(path), headers=bearer(caller))

    exempt = route in _CONSENT_EXEMPT_ROUTES
    refused = (
        response.status_code == status.HTTP_403_FORBIDDEN
        and response.json().get("type") == TermsAcceptanceRequiredError.type
    )
    assert refused is not exempt, (
        f"{method} {path} returned {response.status_code} for a caller owing an acceptance. "
        f"Every authenticated route must refuse one unless it is listed in _CONSENT_EXEMPT_ROUTES "
        f"in {__file__} and in CONSENT_EXEMPT_ENDPOINTS."
    )
    if refused:
        assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("route", sorted(_CONSENT_EXEMPT_ROUTES), ids=_route_id)
async def test_exempt_route_still_answers_the_caller_who_owes_an_acceptance(
    auth_db_client: AsyncClient, db_session: AsyncSession, route: tuple[str, str]
) -> None:
    # The other direction of the same rule: these are what an account uses to clear the state, so a
    # 403 here would trap it with no way out. `GET /terms/current` completes the set and needs no
    # entry — it is public, so it never resolves the dependency that refuses.
    method, path = route
    caller = await caller_with(db_session, *Permission)
    await publish_terms(db_session, version="1.0", content="Terms")

    response = await auth_db_client.request(method, _fill(path), headers=bearer(caller))

    assert response.status_code != status.HTTP_403_FORBIDDEN


async def test_publishing_is_refused_to_an_admin_who_owes_an_acceptance(
    auth_db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # The case that motivated the enforcement: the person owing consent could still perform the
    # platform-wide write that gates everybody else.
    admin = await caller_with(db_session, Permission.PLATFORM_SETTINGS_UPDATE)
    await publish_terms(db_session, version="1.0", content="Terms")

    response = await auth_db_client.post(
        "/api/v1/terms", headers=bearer(admin), json={"version": "2.0", "content": "Second"}
    )

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["type"] == TermsAcceptanceRequiredError.type
