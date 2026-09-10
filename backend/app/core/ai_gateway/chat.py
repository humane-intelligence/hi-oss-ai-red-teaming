"""Provider-agnostic, OpenAI-shape value types — the dispatch layer's I/O contract.

Domain code depends on these, never on raw `litellm` / `openai` objects; adapters
translate. `ChatMessageDelta` is split from `ChatMessage` because a streaming chunk
carries only partial content, so its fields are optional.
"""

from typing import Annotated
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator

Role = Literal["system", "user", "assistant"]


class TextContentPart(BaseModel):
    """A text fragment of a multi-modal message (OpenAI content-part shape)."""

    type: Literal["text"] = "text"
    text: str


class ImageUrl(BaseModel):
    """`image_url` payload — a `data:` URI carrying the image inline.

    Constrained to the `data:` scheme: this type is public via `/chat/stream`, and a
    fetchable URL would let a caller make the provider (or litellm inlining) request an
    arbitrary host from our infrastructure (SSRF). Our write-path always inlines the
    blob as base64 (`read_data_url`), so nothing legitimate needs a remote URL.
    """

    url: str

    @field_validator("url")
    @classmethod
    def _require_data_uri(cls, value: str) -> str:
        if not value.startswith("data:"):
            raise ValueError("must be an inline 'data:' URI, not a fetchable URL")
        return value


class ImageContentPart(BaseModel):
    """An image fragment of a multi-modal message (OpenAI content-part shape)."""

    type: Literal["image_url"] = "image_url"
    image_url: ImageUrl


ContentPart = Annotated[TextContentPart | ImageContentPart, Field(discriminator="type")]


class ChatMessage(BaseModel):
    """One complete message in a conversation.

    `content` is either plain text or, for a multi-modal input, a list of parts
    (text + image); a model reply is always plain text. The list form serialises
    straight into litellm's OpenAI-shape `content` via `model_dump()`, so the
    adapter needs no special handling.
    """

    role: Role
    content: str | list[ContentPart]


class ChatMessageDelta(BaseModel):
    """Incremental slice of a streamed message; both fields optional (role on the first chunk only)."""

    role: Role | None = None
    content: str | None = None


class Usage(BaseModel):
    """Token accounting; None (or partial) when a provider reports little or nothing."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ChatChoice(BaseModel):
    """One generated alternative in a non-streamed completion."""

    index: int
    message: ChatMessage
    finish_reason: str | None = Field(
        default=None,
        description="Why generation stopped (e.g. 'stop', 'length'); None if unknown.",
    )


class ChatCompletion(BaseModel):
    """A non-streamed chat completion — the normalized result of `ModelProvider.chat`."""

    id: str
    model: str = Field(description="Provider-reported model identifier that served the request.")
    created: int = Field(description="Unix epoch seconds when the provider created the completion.")
    choices: list[ChatChoice]
    usage: Usage | None = None


class ChunkChoice(BaseModel):
    """One generated alternative within a streamed chunk."""

    index: int
    delta: ChatMessageDelta
    finish_reason: str | None = None


class ChatChunk(BaseModel):
    """A streamed chunk yielded by `ModelProvider.stream`; `usage` set only on the terminal chunk, if at all."""

    id: str
    model: str
    created: int
    choices: list[ChunkChoice]
    usage: Usage | None = None
