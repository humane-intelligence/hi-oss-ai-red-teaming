"""Tests for ScopedSessionMiddleware — only engages on configured path prefixes.

Strategy: build a minimal FastAPI app with the middleware applied, then drive
it with httpx's in-memory ASGI transport. Inside the configured prefix,
`request.session` is mutable and SessionMiddleware emits a `Set-Cookie`.
Outside it, `scope["session"]` is absent — proof that `SessionMiddleware`
never ran for that request.
"""

import pytest
from fastapi import FastAPI
from fastapi import Request
from httpx import ASGITransport
from httpx import AsyncClient

from app.core.middleware import ScopedSessionMiddleware

SECRET = "x" * 32
COOKIE = "scoped_cookie"


def _build_app() -> FastAPI:
    fastapi_app = FastAPI()
    fastapi_app.add_middleware(
        ScopedSessionMiddleware,
        prefixes=("/scoped",),
        secret_key=SECRET,
        session_cookie=COOKIE,
    )

    @fastapi_app.get("/scoped/inside")
    async def _inside(request: Request) -> dict[str, bool]:
        request.session["k"] = "v"
        return {"ok": True}

    @fastapi_app.get("/other")
    async def _outside(request: Request) -> dict[str, bool]:
        return {"has_session": "session" in request.scope}

    @fastapi_app.get("/scopedish/outside")
    async def _lookalike(request: Request) -> dict[str, bool]:
        return {"has_session": "session" in request.scope}

    return fastapi_app


@pytest.mark.unit
async def test_session_engages_on_matching_prefix() -> None:
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/scoped/inside")

    assert response.status_code == 200
    set_cookies = response.headers.get_list("set-cookie")
    assert any(f"{COOKIE}=" in v for v in set_cookies)


@pytest.mark.unit
async def test_session_bypassed_for_non_matching_path() -> None:
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/other")

    assert response.status_code == 200
    assert response.json() == {"has_session": False}
    assert response.headers.get_list("set-cookie") == []


@pytest.mark.unit
async def test_session_bypassed_for_prefix_lookalike() -> None:
    """`/scopedish/...` must not match the `/scoped` prefix."""
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/scopedish/outside")

    assert response.status_code == 200
    assert response.json() == {"has_session": False}
