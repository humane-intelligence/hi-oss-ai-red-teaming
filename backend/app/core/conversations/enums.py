"""Closed-set enums for conversation messages — surfaced on the wire and in DB enum columns."""

from enum import StrEnum


class MessageRole(StrEnum):
    """Author of a message. Closed set — adding a value needs a migration (`ALTER TYPE ... ADD VALUE`)."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageStatus(StrEnum):
    """Generation lifecycle of a message, orthogonal to soft-delete (`deleted_at`).

    A user/system message lands `complete`; an assistant placeholder opens
    `streaming` and finalises to `complete`, `interrupted` (handled
    disconnect/timeout — partial text kept), or `error` (model failure). The set
    is closed — adding a value needs a migration (`ALTER TYPE ... ADD VALUE`).
    """

    STREAMING = "streaming"
    COMPLETE = "complete"
    INTERRUPTED = "interrupted"
    ERROR = "error"
