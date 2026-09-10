"""SESBackend — transactional email via the AWS SESv2 API."""

from email import policy

import boto3
from botocore.exceptions import ClientError
from botocore.exceptions import ConnectionError as BotoConnectionError
from botocore.exceptions import ReadTimeoutError

from app.core.config import get_settings
from app.core.email.backends.base import EmailMessage
from app.core.email.backends.base import TransientEmailError
from app.core.email.backends.mime import build_mime

_SERVER_ERROR_STATUS = 500

# SES error codes worth retrying; any other code is a permanent rejection.
_TRANSIENT_SES_CODES = frozenset({"ThrottlingException", "TooManyRequestsException"})


class SESBackend:
    """Send mail through AWS SESv2 (`send_email` with Raw content).

    Raw (a full MIME message) rather than Simple so inline images (the brand
    logo, referenced by `cid:`) ride along as `multipart/related` parts.
    Credentials come from the default boto3 chain (instance profile in prod,
    env vars locally); only the region is passed explicitly.
    """

    def send(self, message: EmailMessage) -> str:
        settings = get_settings()
        client = boto3.client("sesv2", region_name=settings.aws_region)
        try:
            response = client.send_email(
                FromEmailAddress=message.from_addr,
                Destination={"ToAddresses": [message.to]},
                Content={"Raw": {"Data": build_mime(message).as_bytes(policy=policy.SMTP)}},
            )
        except (BotoConnectionError, ReadTimeoutError) as exc:
            raise TransientEmailError(str(exc)) from exc
        except ClientError as exc:
            if _is_transient(exc):
                raise TransientEmailError(str(exc)) from exc
            raise
        return response["MessageId"]


def _is_transient(exc: ClientError) -> bool:
    """A SES ClientError is retryable on throttling or any 5xx response."""
    if exc.response.get("Error", {}).get("Code") in _TRANSIENT_SES_CODES:
        return True
    return exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) >= _SERVER_ERROR_STATUS
