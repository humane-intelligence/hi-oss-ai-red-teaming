"""Every `APIRouter` defined under `app.api` must actually be mounted on the app.

A router module whose `include_router` call was forgotten serves nothing and no
endpoint test fails — its own tests would drive the router object directly. This
walks the package tree and asserts every route of every module-level router is
reachable from `app.main.app`.
"""

import importlib
import pkgutil

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute

import app.api
from scripts.openapi_permissions import effective_routes

pytestmark = pytest.mark.unit


def _module_routers() -> list[tuple[str, APIRouter]]:
    routers = []
    for info in pkgutil.walk_packages(app.api.__path__, prefix="app.api."):
        module = importlib.import_module(info.name)
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            routers.append((info.name, router))
    return routers


def test_every_api_router_is_mounted() -> None:
    mounted = {route.endpoint for _, route in effective_routes()}

    violations = [
        f"{module_name}: {route.path}"
        for module_name, router in _module_routers()
        for route in router.routes
        if isinstance(route, APIRoute) and route.endpoint not in mounted
    ]

    assert not violations, f"routers defined but never include_router-ed: {violations}"
