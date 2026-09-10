"""Unit tests for the ProviderError hierarchy."""

import pytest

from app.core.ai_gateway.exceptions import ProviderAuthError
from app.core.ai_gateway.exceptions import ProviderBadRequestError
from app.core.ai_gateway.exceptions import ProviderContextWindowError
from app.core.ai_gateway.exceptions import ProviderError
from app.core.ai_gateway.exceptions import ProviderRateLimitError
from app.core.ai_gateway.exceptions import ProviderTimeoutError
from app.core.ai_gateway.exceptions import ProviderUnavailableError

_SUBCLASSES = [
    ProviderRateLimitError,
    ProviderAuthError,
    ProviderTimeoutError,
    ProviderBadRequestError,
    ProviderContextWindowError,
    ProviderUnavailableError,
]


@pytest.mark.unit
@pytest.mark.parametrize("exc_type", _SUBCLASSES)
def test_every_subclass_is_a_provider_error(exc_type: type[ProviderError]) -> None:
    assert issubclass(exc_type, ProviderError)


@pytest.mark.unit
def test_context_window_is_not_a_bad_request() -> None:
    # The caller's remedy differs, so a context-window overflow must be catchable
    # without also swallowing generic bad-request errors.
    assert not issubclass(ProviderContextWindowError, ProviderBadRequestError)


@pytest.mark.unit
def test_carries_message_and_chains_cause() -> None:
    cause = ValueError("boom")
    with pytest.raises(ProviderError, match="slow down") as exc_info:
        raise ProviderRateLimitError("slow down") from cause
    assert exc_info.value.__cause__ is cause
