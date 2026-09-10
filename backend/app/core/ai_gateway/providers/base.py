"""The `ModelProvider` port — the call abstraction domain code depends on.

Methods take semantic inputs (vendor + provider-side model id + key/endpoint/
params), not a library-specific model string; each adapter translates.
`@runtime_checkable` lets tests assert conformance via `isinstance`.
"""

from collections.abc import AsyncIterator
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatCompletion
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.enums import ProviderVendor


@runtime_checkable
class ModelProvider(Protocol):
    """Port every concrete provider adapter implements."""

    async def chat(
        self,
        *,
        vendor: ProviderVendor,
        provider_model_id: str,
        messages: list[ChatMessage],
        api_key: str | None = None,
        api_base: str | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — forwarded to the provider's own timeout
    ) -> ChatCompletion:
        """Return a single completion for ``messages``."""

    def stream(
        self,
        *,
        vendor: ProviderVendor,
        provider_model_id: str,
        messages: list[ChatMessage],
        api_key: str | None = None,
        api_base: str | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """Yield completion chunks as they arrive from the provider."""
