"""Closed-set enums for review rows — surfaced both on the wire and in DB enum columns."""

from enum import StrEnum


class ReviewStatus(StrEnum):
    """Lifecycle of a single `Review`.

    A review lands `pending` when a reviewer is assigned and moves to `approved`
    / `rejected` when they submit a verdict. The set is closed — adding a state
    requires a migration (`ALTER TYPE ... ADD VALUE`).
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
