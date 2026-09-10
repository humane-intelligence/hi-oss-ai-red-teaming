"""Closed-set enums for annotation rows — surfaced both on the wire and in DB enum columns."""

from enum import StrEnum


class FlagStatus(StrEnum):
    """Review lifecycle of a `MessageFlag`.

    A flag lands `pending` and is moved to `approved` / `rejected` by the review
    workflow (a separate scope — there is no transition endpoint yet, so today it
    is always `pending`). The set is closed — adding a state requires a migration
    (`ALTER TYPE ... ADD VALUE`).
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
