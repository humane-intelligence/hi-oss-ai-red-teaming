"""ConsoleBackend — no-network default for dev / tests."""

from app.core.email.backends.base import EmailMessage
from app.core.logging import get_logger

logger = get_logger(__name__)


class ConsoleBackend:
    """Backend that only logs — no network."""

    def send(self, message: EmailMessage) -> None:
        """Log the email as a single structured event."""
        logger.info(
            "email.sent.console",
            backend="console",
            to=message.to,
            from_addr=message.from_addr,
            subject=message.subject,
            body_text=message.body_text,
            body_html=message.body_html,
            inline_images=[image.cid for image in message.inline_images],
        )
