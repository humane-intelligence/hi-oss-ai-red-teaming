"""Which provider/model pairs may carry `web_access`, the gateway's native web-tools switch.

Anthropic and Google expose web search on every current model, so the vendor alone decides.
OpenAI only on some models (the `*-search-preview` line and the newer families), so litellm's
per-model capability map decides there. The remaining vendors have no option litellm can
translate `web_search_options` into. The map is not consulted for Anthropic or Google because
it is keyed by exact model ids and misses their aliases (`claude-sonnet-4-5` unknown, the dated
id supported), which would wrongly lock the switch on most Claude rows.
"""

from app.core.ai_gateway.enums import ProviderVendor

WEB_ACCESS_UNSUPPORTED_DETAIL = "This model has no internet access option at its provider."

_VENDOR_WIDE = frozenset({ProviderVendor.ANTHROPIC, ProviderVendor.GOOGLE})


def supports_web_access(vendor: ProviderVendor, provider_model_id: str) -> bool:
    if vendor in _VENDOR_WIDE:
        return True
    if vendor == ProviderVendor.OPENAI:
        import litellm  # noqa: PLC0415 — lazy: keeps the CRUD import path litellm-free

        return bool(litellm.supports_web_search(f"openai/{provider_model_id}"))
    return False
