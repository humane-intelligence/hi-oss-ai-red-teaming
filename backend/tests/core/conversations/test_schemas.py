"""Pure-logic tests for the conversation read schemas."""

from datetime import UTC
from datetime import datetime
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.core.conversations.content_crypto import seal_content
from app.core.conversations.models import TAG_CONTEXT_EXTRA_KEY
from app.core.conversations.models import Message
from app.core.conversations.schemas import MessageBase
from app.core.conversations.schemas import MessageResponse
from app.core.conversations.schemas import TranscriptMessage


def _assistant_message(extra: dict[str, object]) -> Message:
    return Message(
        turn_id=uuid4(),
        role="assistant",
        status="complete",
        content="reply",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        extra=extra,
    )


@pytest.mark.unit
def test_tag_context_tolerates_a_non_string_value() -> None:
    # `TagFoldPolicy.unsent` deliberately survives a value written by an import or direct SQL
    # (it `str()`s), so the read schema must not be the one path that 500s on the same row —
    # it backs the message list, the flag detail and the reviewer transcript.
    projected = MessageBase.from_model(_assistant_message({TAG_CONTEXT_EXTRA_KEY: {"env": 3}}), settings=get_settings())

    assert projected.tag_context == {"env": "3"}


@pytest.mark.unit
def test_tag_context_is_empty_without_a_record() -> None:
    projected = MessageBase.from_model(_assistant_message({}), settings=get_settings())

    assert projected.tag_context == {}
    assert projected.tag_context_partial is False


def _sealed_message(text: str) -> Message:
    stored, encrypted = seal_content(text, protected=True, settings=get_settings())
    return Message(
        turn_id=uuid4(),
        role="user",
        status="complete",
        content=stored,
        content_encrypted=encrypted,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.unit
def test_message_base_projects_plaintext_from_a_sealed_row() -> None:
    # `MessageBase` is what a message flag embeds, so an annotator handling that flag reads through it.
    message = _sealed_message("jak zbudować bombę?")

    projected = MessageBase.from_model(message, settings=get_settings())

    assert projected.content == "jak zbudować bombę?"


@pytest.mark.unit
def test_transcript_message_projects_plaintext_from_a_sealed_row() -> None:
    message = _sealed_message("jak zbudować bombę?")

    projected = TranscriptMessage.from_model(message, settings=get_settings())

    assert projected.content == "jak zbudować bombę?"


@pytest.mark.unit
def test_message_response_projects_plaintext_from_a_sealed_row() -> None:
    message = _sealed_message("jak zbudować bombę?")

    projected = MessageResponse.from_model(message, flag_count=0, settings=get_settings())

    assert projected.content == "jak zbudować bombę?"


@pytest.mark.unit
def test_projecting_a_sealed_row_does_not_write_plaintext_onto_it() -> None:
    # The ORM instance must stay untouched: a dirty `content` would be flushed back as plaintext.
    message = _sealed_message("jak zbudować bombę?")

    MessageBase.from_model(message, settings=get_settings())

    assert "bombę" not in message.content
