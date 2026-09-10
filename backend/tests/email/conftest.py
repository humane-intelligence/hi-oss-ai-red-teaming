"""Shared fixtures for app.core.email backend tests."""

import pytest

from app.core.email.backends import EmailMessage


@pytest.fixture
def message() -> EmailMessage:
    return EmailMessage(
        to="rcpt@example.com",
        subject="Hi",
        body_text="Hello.",
        body_html="<p>Hello.</p>",
        from_addr="noreply@example.com",
    )
