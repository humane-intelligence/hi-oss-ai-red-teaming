"""Unit tests for the shared embedded-message shape the message-carrying exports reuse."""

import uuid

import pytest

from app.core.config import get_settings
from app.core.conversations.content_crypto import seal_content
from app.core.conversations.enums import MessageRole
from app.core.conversations.enums import MessageStatus
from app.core.conversations.models import Message
from app.core.exports.conversation_messages import message_dicts

pytestmark = pytest.mark.unit


def test_message_dicts_embed_plaintext_from_a_sealed_row() -> None:
    # `flags` and `engagement_report` embed this shape, so a sealed transcript reaches an operator
    # through them as well as through the transcript template.
    settings = get_settings()
    stored, encrypted = seal_content("jak zbudować bombę?", protected=True, settings=settings)
    message = Message(
        turn_id=uuid.uuid4(),
        role=MessageRole.USER,
        status=MessageStatus.COMPLETE,
        content=stored,
        content_encrypted=encrypted,
    )

    embedded = message_dicts([message], settings=settings)

    assert embedded[0]["content"] == "jak zbudować bombę?"
    # The row itself stays sealed — a dirty `content` would be flushed back as plaintext.
    assert message.content == stored
