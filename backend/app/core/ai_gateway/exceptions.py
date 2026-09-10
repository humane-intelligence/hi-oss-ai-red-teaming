"""Domain failure taxonomy for provider calls.

Adapters map upstream `litellm` / `openai` exceptions onto this hierarchy so the
rest of the app imports neither. Subclasses are markers the caller branches on
(retry, truncate, surface). Not an `APIError`: a provider failure isn't inherently
an HTTP concern — the consuming route maps it to a status when one exists.
"""


class ProviderError(Exception):
    """Base for any failure surfaced from a provider call."""


class ProviderRateLimitError(ProviderError):
    """Provider rejected the request for exceeding a rate or quota limit (HTTP 429 upstream)."""


class ProviderAuthError(ProviderError):
    """Provider rejected the credentials — missing, invalid, or unauthorized for the model."""


class ProviderTimeoutError(ProviderError):
    """The call did not complete within the configured timeout."""


class ProviderBadRequestError(ProviderError):
    """Provider rejected the request as malformed (bad params, unknown model, invalid input)."""


class ProviderContextWindowError(ProviderError):
    """Input exceeded the context window — split from BadRequest so the caller can truncate-and-retry."""


class ProviderUnavailableError(ProviderError):
    """Provider unreachable / transient failure (5xx, connection drop, or undecryptable stored credential)."""
