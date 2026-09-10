"""Annotate an OpenAPI spec with the required permission of each gated operation.

Kept out of ``dump_openapi.py`` on purpose: the canonical ``docs/openapi.yaml`` is a
faithful dump of the live FastAPI schema and must not carry derived prose. The required
permission is a Postman-mirror concern, so the introspection and the description-injection
live here and run only on the ``dump_postman`` path.

The permission gates (`require_permission`, `require_group_permission`) capture a
`Permission` in a closure, so it cannot be read off the schema — we introspect the resolved
route dependencies for it. The object-scope *visibility* gate references its permission as a
module global rather than a closure, so a few read endpoints are not annotated here — their
403 response text already names the permission.

Route traversal goes through `_IncludedRouter.effective_route_contexts()` (the same
flattening FastAPI's own schema generation uses), since modern FastAPI no longer flattens
`include_router` into `app.routes`. A single pass (`introspect_routes`) yields both the
required-permission map and the public-route set. An introspection failure **raises** rather
than silently shipping a degraded mirror: the output is committed and drift-gated, so a loud
`make postmandump` failure is better than a quietly stripped artifact.
"""

from collections.abc import Iterator
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.core.auth.dependencies import current_user
from app.core.auth.roles import Permission
from app.core.logging import get_logger
from app.main import app

logger = get_logger(__name__)

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _permissions_in_dependant(dependant: Dependant) -> set[str]:
    """Walk the dependency tree collecting any `Permission` captured in a gate's closure."""
    found: set[str] = set()
    for dependency in dependant.dependencies:
        for cell in getattr(dependency.call, "__closure__", None) or ():
            try:
                value = cell.cell_contents
            except ValueError:  # an empty cell mid-construction
                continue
            if isinstance(value, Permission):
                found.add(value.value)
        found |= _permissions_in_dependant(dependency)
    return found


def effective_routes() -> Iterator[tuple[str, APIRoute]]:
    """Yield `(full_path, route)` for every API route, descending through included routers.

    Modern FastAPI wraps `include_router` results in `_IncludedRouter` (a private type)
    rather than flattening into `app.routes`; `effective_route_contexts()` is the same
    flattener its schema generation uses. Accessed via `getattr` to stay tolerant of the
    internal shape and to keep the type checker from choking on the private API.
    """
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route.path_format, route
            continue
        contexts_getter = getattr(route, "effective_route_contexts", None)  # _IncludedRouter
        if contexts_getter is None:
            continue
        for context in contexts_getter():
            original = getattr(context, "original_route", None)
            if isinstance(original, APIRoute):
                yield getattr(context, "path"), original  # noqa: B009 — dynamic on a private type


def _depends_on_current_user(dependant: Dependant) -> bool:
    """True if the route reaches `current_user` (directly or via `require_permission`)."""
    for dependency in dependant.dependencies:
        if dependency.call is current_user or _depends_on_current_user(dependency):
            return True
    return False


def introspect_routes() -> tuple[dict[tuple[str, str], list[str]], set[tuple[str, str]]]:
    """One pass over the live routes → `(required-permissions map, public-route set)`.

    A single `effective_routes()` traversal feeds both Postman-mirror enrichments, so the
    private `_IncludedRouter` flattening runs once and the two consumers can't drift:

    * **permissions** — `(METHOD, path) -> sorted permission keys` for the
      `**Requires permission:**` note (a key is read off a gate's closure).
    * **public** — routes with no `current_user` in their dependency tree. Auth is enforced
      in `AuthMiddleware`; `current_user` is the in-route accessor every authenticated
      handler depends on (directly, e.g. `GET /auth/me`, or via `require_permission`), so a
      route that never reaches it takes no bearer token. New public routes are picked up
      automatically — no hand-maintained list.

    Raises whatever the introspection raises (FastAPI internals changing shape, etc.) — the
    caller (`make postmandump`) must fail loudly rather than emit a degraded mirror.
    """
    permissions: dict[tuple[str, str], list[str]] = {}
    public: set[tuple[str, str]] = set()
    for path, route in effective_routes():
        found = _permissions_in_dependant(route.dependant)
        authenticated = _depends_on_current_user(route.dependant)
        for method in route.methods or set():
            key = (method.upper(), path)
            if key[0] not in _METHODS:
                continue
            if found:
                permissions[key] = sorted(found)
            if not authenticated:
                public.add(key)
    return permissions, public


def annotate_permissions(schema: dict[str, Any], permissions: dict[tuple[str, str], list[str]]) -> None:
    """Append a `**Requires permission:**` note to each gated operation's description, in place.

    ``permissions`` is the `(METHOD, path) -> keys` map from `introspect_routes`; mutates
    ``schema`` (an already-loaded OpenAPI dict).
    """
    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            keys = permissions.get((method.upper(), path)) if method.upper() in _METHODS else None
            if not keys:
                continue
            label = "permission" if len(keys) == 1 else "permissions"
            note = f"**Requires {label}:** " + ", ".join(f"`{key}`" for key in keys)
            existing = operation.get("description", "") or ""
            operation["description"] = f"{existing}\n\n{note}".strip()
