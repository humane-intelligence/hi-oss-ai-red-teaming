"""Enumerations for the notifications domain."""

from enum import StrEnum


class NotificationObjectType(StrEnum):
    """Data model a notification points at (its `object_id` names the row).

    Renaming or removing a member is hazardous: existing rows carrying that
    value would fail `NotificationObjectType(...)` on read — purge or migrate
    those rows first.
    """

    EVALUATION = "evaluation"
    EVALUATION_GROUP = "evaluation_group"
    AI_MODEL = "ai_model"
