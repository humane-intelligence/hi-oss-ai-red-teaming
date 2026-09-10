"""Enumerations for the saved-views domain."""

from enum import StrEnum


class SavedViewResource(StrEnum):
    """List views a saved view can be scoped to.

    The value is the stable client-facing key the frontend keys each list page
    to. The backend treats a view's `state` as opaque, so this is the only
    per-list vocabulary — making a new list savable is one member here, not a
    migration (the column is a plain string). Removing or renaming a member is
    the hazardous direction: existing rows carrying that value would fail
    `SavedViewResource(...)` on read — purge or migrate those rows first.
    """

    EVALUATIONS = "evaluations"
    EVALUATION_GROUPS = "evaluation-groups"
    CONVERSATIONS = "conversations"
    CONVERSATION_GROUPS = "conversation-groups"
    MESSAGE_FLAGS = "message-flags"
    AI_MODELS = "ai-models"
    USERS = "users"
    ORGANIZATIONS = "organizations"
    REVIEWS = "reviews"
    AUDIT_LOGS = "audit-logs"
