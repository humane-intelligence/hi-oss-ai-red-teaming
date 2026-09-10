"""Unit tests for the chat value types — construction, defaults, and roundtrip."""

import pytest

from app.core.ai_gateway.chat import ChatChoice
from app.core.ai_gateway.chat import ChatChunk
from app.core.ai_gateway.chat import ChatCompletion
from app.core.ai_gateway.chat import ChatMessage
from app.core.ai_gateway.chat import ChatMessageDelta
from app.core.ai_gateway.chat import ChunkChoice
from app.core.ai_gateway.chat import Usage


@pytest.mark.unit
def test_chat_completion_roundtrips_through_dict() -> None:
    completion = ChatCompletion(
        id="cmpl-1",
        model="gpt-4o",
        created=1_700_000_000,
        choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content="hi"), finish_reason="stop")],
        usage=Usage(prompt_tokens=3, completion_tokens=1, total_tokens=4),
    )

    assert ChatCompletion.model_validate(completion.model_dump()) == completion


@pytest.mark.unit
def test_completion_usage_and_finish_reason_default_to_none() -> None:
    completion = ChatCompletion(
        id="cmpl-2",
        model="gpt-4o",
        created=1,
        choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content="x"))],
    )

    assert completion.usage is None
    assert completion.choices[0].finish_reason is None


@pytest.mark.unit
def test_chunk_delta_fields_are_optional() -> None:
    chunk = ChatChunk(
        id="chunk-1",
        model="gpt-4o",
        created=1,
        choices=[ChunkChoice(index=0, delta=ChatMessageDelta())],
    )

    assert chunk.choices[0].delta.role is None
    assert chunk.choices[0].delta.content is None
    assert chunk.usage is None


@pytest.mark.unit
def test_chat_message_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="role"):
        ChatMessage(role="tool", content="x")  # ty: ignore[invalid-argument-type]
