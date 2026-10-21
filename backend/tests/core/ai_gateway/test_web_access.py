"""Unit tests for the web-access capability rule."""

import subprocess
import sys
from pathlib import Path

import litellm
import pytest

from app.core.ai_gateway.enums import ProviderVendor
from app.core.ai_gateway.web_access import supports_web_access


@pytest.mark.unit
@pytest.mark.parametrize("vendor", [ProviderVendor.ANTHROPIC, ProviderVendor.GOOGLE])
def test_vendor_wide_support_ignores_the_model_id(vendor: ProviderVendor) -> None:
    assert supports_web_access(vendor, "not-in-any-capability-map")


@pytest.mark.unit
@pytest.mark.parametrize(
    "vendor",
    [
        ProviderVendor.AZURE,
        ProviderVendor.AWS_BEDROCK,
        ProviderVendor.COHERE,
        ProviderVendor.HUGGINGFACE,
        ProviderVendor.GENERIC,
    ],
)
def test_vendors_without_a_web_option_are_unsupported(vendor: ProviderVendor) -> None:
    assert not supports_web_access(vendor, "gpt-4o-search-preview")


@pytest.mark.unit
@pytest.mark.parametrize("answer", [True, False])
def test_openai_defers_to_litellm_per_model(monkeypatch: pytest.MonkeyPatch, answer: bool) -> None:
    asked: list[str] = []

    def _supports(model: str, custom_llm_provider: str | None = None) -> bool:
        asked.append(model)
        return answer

    monkeypatch.setattr(litellm, "supports_web_search", _supports)

    assert supports_web_access(ProviderVendor.OPENAI, "gpt-4o-search-preview") is answer
    assert asked == ["openai/gpt-4o-search-preview"]


# In a subprocess: this test process has litellm loaded already, which would mask the leak.
@pytest.mark.unit
def test_importing_the_module_keeps_litellm_out() -> None:
    probe = "import sys; import app.core.ai_gateway.web_access; print('litellm' in sys.modules)"
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell, no user input
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[3],
    )

    assert result.stdout.strip() == "False"
