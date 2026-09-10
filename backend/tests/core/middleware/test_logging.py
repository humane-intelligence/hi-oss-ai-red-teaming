"""Tests for LoggingMiddleware — per-request context binding and access log.

Strategy: build a minimal FastAPI app, drive it with httpx's in-memory ASGI
transport, and replace `access_logger` with a fake that captures both the
call kwargs and the live `structlog.contextvars` snapshot at call time.
The middleware's `finally` runs inside `bound_contextvars`, so the snapshot
is what `merge_contextvars` would have folded in during real operation.
"""

import re
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import MutableMapping
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from fastapi import status
from httpx import ASGITransport
from httpx import AsyncClient

from app.core.middleware import logging as logging_module
from app.core.middleware.logging import ACCESS_LOG_EVENT
from app.core.middleware.logging import LoggingMiddleware

REQUEST_ID_HEX = re.compile(r"^[0-9a-f]{32}$")


def _build_app(handler_contextvars: list[dict[str, Any]] | None = None) -> FastAPI:
    fastapi_app = FastAPI()
    fastapi_app.add_middleware(LoggingMiddleware)

    @fastapi_app.get("/ok")
    async def _ok() -> dict[str, bool]:
        if handler_contextvars is not None:
            handler_contextvars.append(dict(structlog.contextvars.get_contextvars()))
        return {"ok": True}

    @fastapi_app.post("/items")
    async def _items() -> dict[str, bool]:
        if handler_contextvars is not None:
            handler_contextvars.append(dict(structlog.contextvars.get_contextvars()))
        return {"ok": True}

    @fastapi_app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("kaboom")

    return fastapi_app


@pytest.fixture(autouse=True)
def access_log_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture `access_logger.info(...)` calls with the live contextvars at call time.

    Autouse so every test in this module silences the real logger; tests that
    want to inspect the captured calls still request the fixture by name.
    """
    calls: list[dict[str, Any]] = []

    class _Capturer:
        @staticmethod
        def info(event: str, **kwargs: Any) -> None:
            calls.append(
                {
                    "event": event,
                    "contextvars": dict(structlog.contextvars.get_contextvars()),
                    **kwargs,
                }
            )

    monkeypatch.setattr(logging_module, "access_logger", _Capturer())
    return calls


@pytest.fixture
def handler_contextvars() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def fastapi_app(handler_contextvars: list[dict[str, Any]]) -> FastAPI:
    return _build_app(handler_contextvars=handler_contextvars)


@pytest.fixture
async def client(fastapi_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client


@pytest.mark.unit
async def test_access_log_emitted_with_status_and_duration(
    client: AsyncClient, access_log_calls: list[dict[str, Any]]
) -> None:
    await client.get("/ok")

    assert len(access_log_calls) == 1
    call = access_log_calls[0]
    assert call["event"] == ACCESS_LOG_EVENT
    assert call["status_code"] == status.HTTP_200_OK
    assert isinstance(call["duration_ms"], float)
    assert call["duration_ms"] >= 0


@pytest.mark.unit
async def test_response_includes_request_id_header(client: AsyncClient) -> None:
    response = await client.get("/ok")

    request_id = response.headers["x-request-id"]
    assert REQUEST_ID_HEX.fullmatch(request_id)


@pytest.mark.unit
async def test_inbound_request_id_is_ignored(client: AsyncClient) -> None:
    response = await client.get("/ok", headers={"X-Request-ID": "attacker-controlled"})

    assert response.headers["x-request-id"] != "attacker-controlled"
    assert REQUEST_ID_HEX.fullmatch(response.headers["x-request-id"])


@pytest.mark.unit
async def test_request_ids_are_unique_across_requests(
    client: AsyncClient, access_log_calls: list[dict[str, Any]]
) -> None:
    r1 = await client.get("/ok")
    r2 = await client.get("/ok")

    assert r1.headers["x-request-id"] != r2.headers["x-request-id"]
    assert access_log_calls[0]["contextvars"]["request_id"] != access_log_calls[1]["contextvars"]["request_id"]


@pytest.mark.unit
async def test_contextvars_bound_during_handler(
    client: AsyncClient,
    handler_contextvars: list[dict[str, Any]],
) -> None:
    await client.get("/ok")

    bound = handler_contextvars[0]
    assert REQUEST_ID_HEX.fullmatch(bound["request_id"])
    assert bound["method"] == "GET"
    assert bound["path"] == "/ok"


@pytest.mark.unit
async def test_access_log_request_id_matches_response_header(
    client: AsyncClient, access_log_calls: list[dict[str, Any]]
) -> None:
    response = await client.get("/ok")

    assert access_log_calls[0]["contextvars"]["request_id"] == response.headers["x-request-id"]


@pytest.mark.unit
async def test_access_log_carries_method_and_path(client: AsyncClient, access_log_calls: list[dict[str, Any]]) -> None:
    await client.post("/items")

    ctx = access_log_calls[0]["contextvars"]
    assert ctx["method"] == "POST"
    assert ctx["path"] == "/items"


@pytest.mark.unit
async def test_unhandled_exception_still_emits_500_access_log(
    client: AsyncClient, access_log_calls: list[dict[str, Any]]
) -> None:
    with pytest.raises(RuntimeError, match="kaboom"):
        await client.get("/boom")

    assert len(access_log_calls) == 1
    assert access_log_calls[0]["event"] == ACCESS_LOG_EVENT
    assert access_log_calls[0]["status_code"] == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert REQUEST_ID_HEX.fullmatch(access_log_calls[0]["contextvars"]["request_id"])


@pytest.mark.unit
async def test_contextvars_unbound_after_request(client: AsyncClient) -> None:
    await client.get("/ok")

    leaked = structlog.contextvars.get_contextvars()
    assert "request_id" not in leaked
    assert "method" not in leaked
    assert "path" not in leaked


@pytest.mark.unit
async def test_non_http_scope_passes_through_without_logging(
    access_log_calls: list[dict[str, Any]],
) -> None:
    inner_scopes: list[str] = []

    async def inner_app(
        scope: MutableMapping[str, Any],
        _receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        _send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        inner_scopes.append(scope["type"])

    middleware = LoggingMiddleware(inner_app)

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "lifespan.startup"}

    async def send(_message: MutableMapping[str, Any]) -> None:
        return None

    await middleware({"type": "lifespan"}, receive, send)

    assert inner_scopes == ["lifespan"]
    assert access_log_calls == []
