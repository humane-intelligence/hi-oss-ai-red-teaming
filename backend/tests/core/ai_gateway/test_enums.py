import pytest

from app.core.ai_gateway.enums import LITELLM_PREFIX
from app.core.ai_gateway.enums import ProviderVendor


@pytest.mark.unit
def test_litellm_prefix_covers_every_vendor() -> None:
    # A vendor added to the enum without a prefix would KeyError at dispatch and in the save-time rule.
    assert set(LITELLM_PREFIX) == set(ProviderVendor)
