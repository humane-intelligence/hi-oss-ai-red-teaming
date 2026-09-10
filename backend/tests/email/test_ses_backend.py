"""Tests for app.core.email.backends.ses.SESBackend — mocked SESv2 client."""

from email import message_from_bytes
from email import policy
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from botocore.exceptions import EndpointConnectionError

from app.core.email.backends import ses as ses_module
from app.core.email.backends.base import EmailMessage
from app.core.email.backends.base import InlineImage
from app.core.email.backends.base import TransientEmailError
from app.core.email.backends.ses import SESBackend

_PNG = b"\x89PNG\r\n\x1a\nfake-logo-bytes"


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    monkeypatch.setattr(ses_module.boto3, "client", lambda *_a, **_k: client)


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        error_response={"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation_name="SendEmail",
    )


@pytest.mark.unit
def test_ses_backend_sends_and_returns_message_id(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    client = MagicMock()
    client.send_email.return_value = {"MessageId": "ses-abc-123"}
    _patch_client(monkeypatch, client)

    result = SESBackend().send(message)

    assert result == "ses-abc-123"
    kwargs = client.send_email.call_args.kwargs
    assert kwargs["FromEmailAddress"] == "noreply@example.com"
    assert kwargs["Destination"] == {"ToAddresses": ["rcpt@example.com"]}
    parsed = message_from_bytes(kwargs["Content"]["Raw"]["Data"], policy=policy.default)
    assert parsed["Subject"] == "Hi"
    bodies = {part.get_content_type(): part.get_content().strip() for part in parsed.iter_parts()}
    assert bodies == {"text/plain": "Hello.", "text/html": "<p>Hello.</p>"}


@pytest.mark.unit
def test_ses_backend_raw_embeds_inline_image(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    client = MagicMock()
    client.send_email.return_value = {"MessageId": "x"}
    _patch_client(monkeypatch, client)
    with_logo = message.model_copy(update={"inline_images": [InlineImage(cid="logo", data=_PNG)]})

    SESBackend().send(with_logo)

    parsed = message_from_bytes(client.send_email.call_args.kwargs["Content"]["Raw"]["Data"], policy=policy.default)
    images = [part for part in parsed.walk() if part.get_content_maintype() == "image"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == "<logo>"
    assert images[0].get_content_type() == "image/png"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "status"),
    [("ThrottlingException", 400), ("TooManyRequestsException", 429), ("InternalFailure", 500)],
)
def test_ses_backend_maps_transient_errors(
    monkeypatch: pytest.MonkeyPatch, message: EmailMessage, code: str, status: int
) -> None:
    client = MagicMock()
    client.send_email.side_effect = _client_error(code, status)
    _patch_client(monkeypatch, client)

    with pytest.raises(TransientEmailError):
        SESBackend().send(message)


@pytest.mark.unit
def test_ses_backend_reraises_permanent_rejection(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    client = MagicMock()
    client.send_email.side_effect = _client_error("MessageRejected", 400)
    _patch_client(monkeypatch, client)

    with pytest.raises(ClientError):
        SESBackend().send(message)


@pytest.mark.unit
def test_ses_backend_connection_error_is_transient(monkeypatch: pytest.MonkeyPatch, message: EmailMessage) -> None:
    client = MagicMock()
    client.send_email.side_effect = EndpointConnectionError(endpoint_url="https://email.us-east-1.amazonaws.com")
    _patch_client(monkeypatch, client)

    with pytest.raises(TransientEmailError):
        SESBackend().send(message)
