"""End-to-end: a real self-hosted SLM round-trips through the gateway.

Opt-in. Exercises the live `slm` compose service (llama.cpp, OpenAI-compatible)
end to end: register an `AiModel` (provider=generic) → `dispatch_chat`/
`dispatch_stream` → live model. Skipped unless that server is reachable, so plain
`make test` and CI (which don't start the `llm` profile) stay green.

Scope is intentionally the dispatch layer, not the `/chat/stream` SSE endpoint:
the transport translation is exercised elsewhere with synthetic chunks, so the
live-model check stays focused on real provider round-tripping.

Bring it up and run:

    make upllm                    # starts the SLM; first boot downloads the GGUF
    make test ARGS='-m e2e'

The host-run test can't resolve the in-network `slm` hostname the seeded
`local-slm` row uses, and `--disable-socket` only allows loopback, so this
registers its own row pointing at the host-published port (127.0.0.1:8081 by
default; override via SLM_E2E_BASE_URL / SLM_E2E_MODEL / SLM_E2E_API_KEY). A
non-loopback SLM_E2E_BASE_URL must also be allowed through pytest-socket, e.g.
`make test ARGS='--allow-hosts=127.0.0.1,::1,my-host -m e2e'`.
"""

import os
from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_socket
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.dispatch import dispatch_chat
from app.core.ai_gateway.dispatch import dispatch_stream
from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.services.ai_models import create_model
from app.core.config import get_settings
from scripts.seed_local import SLM_PROVIDER_MODEL_ID

pytestmark = pytest.mark.e2e

_BASE_URL = os.environ.get("SLM_E2E_BASE_URL", "http://127.0.0.1:8081/v1")
_MODEL_ID = os.environ.get("SLM_E2E_MODEL", SLM_PROVIDER_MODEL_ID)
_API_KEY = os.environ.get("SLM_E2E_API_KEY")  # falls back to settings.slm_api_key in _register


def _session_provider(session: AsyncSession) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """A stamp-session provider yielding the test session — the app engine has no lifespan here."""

    @asynccontextmanager
    async def provider() -> AsyncIterator[AsyncSession]:
        yield session

    return provider


def _health_url(base: str) -> str:
    # /health sits at the server root, a sibling of the /v1 OpenAI base.
    return base.rstrip("/").removesuffix("/v1").rstrip("/") + "/health"


@pytest.fixture(scope="module", autouse=True)
def _require_live_slm() -> None:
    try:
        httpx.get(_health_url(_BASE_URL), timeout=2.0).raise_for_status()
    # pytest-socket raises SocketConnectBlockedError (a RuntimeError, not OSError) when
    # SLM_E2E_BASE_URL is overridden to a host outside --allow-hosts — skip, don't error.
    except (httpx.HTTPError, OSError, pytest_socket.SocketBlockedError, pytest_socket.SocketConnectBlockedError) as exc:
        pytest.skip(f"self-hosted SLM not reachable at {_BASE_URL} ({exc}); start it with `make upllm`")


async def _register(session: AsyncSession) -> str:
    settings = get_settings()
    alias = "e2e-slm"
    api_key = SecretStr(_API_KEY) if _API_KEY else settings.slm_api_key
    await create_model(
        session,
        settings,
        name="E2E SLM",
        model_alias=alias,
        provider=ProviderVendor.GENERIC,
        provider_model_id=_MODEL_ID,
        inference_endpoint=_BASE_URL,
        api_key=api_key,
    )
    return alias


async def test_dispatch_chat_round_trips_through_self_hosted_slm(db_session: AsyncSession) -> None:
    alias = await _register(db_session)

    completion = await dispatch_chat(
        db_session,
        get_settings(),
        model_alias=alias,
        messages=[ChatMessage(role="user", content="Reply with the single word: pong")],
        params={"temperature": 0.0, "max_tokens": 16},
        timeout=60.0,
        session_provider=_session_provider(db_session),
    )

    assert completion.choices
    assert completion.choices[0].message.role == "assistant"
    content = completion.choices[0].message.content
    assert isinstance(content, str)
    assert content.strip()


# litellm's OpenAI streaming path reads `model_fields` off a pydantic instance,
# which pydantic 2.11 deprecates (`PydanticDeprecatedSince211`). Under the suite-
# wide `filterwarnings=error` that becomes a mid-stream error, so streaming (and
# only streaming) fails inside the third-party SDK. The warning is attributed to
# the openai SDK frame, so pyproject's `:pydantic.*` module ignore never matches
# it — scope the ignore to that pydantic-2.11 deprecation class for this one
# live-streaming test (production never runs warnings-as-errors).
@pytest.mark.filterwarnings("ignore::pydantic.warnings.PydanticDeprecatedSince211")
async def test_dispatch_stream_yields_content_from_self_hosted_slm(db_session: AsyncSession) -> None:
    alias = await _register(db_session)

    stream = await dispatch_stream(
        db_session,
        get_settings(),
        model_alias=alias,
        messages=[ChatMessage(role="user", content="Count from one to three.")],
        params={"temperature": 0.0, "max_tokens": 16},
        timeout=60.0,
        session_provider=_session_provider(db_session),
    )
    content = "".join([chunk.choices[0].delta.content or "" async for chunk in stream if chunk.choices])

    assert content.strip()
