"""Single-use, link-borne tokens shared by invitations and email verification.

Raw tokens live only in memory long enough to compose the outgoing URL; the
DB stores the SHA-256 hex digest so a database leak cannot be turned into
usable links.

Pure stdlib, no session and no I/O despite sitting under `services/` — which is
why `schemas.py` importing `project_expired` from here is not the layer inversion
it looks like, and why moving that one function out to earn a tidier import graph
would buy a module and cost four call sites.
"""

import hashlib
import secrets
from datetime import UTC
from datetime import datetime
from enum import StrEnum

_TOKEN_BYTES = 32


def generate_raw_token() -> str:
    """Return a fresh URL-safe token with ~256 bits of entropy."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(raw_token: str) -> str:
    """Return the digest stored alongside the row; lookups re-hash and compare."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def project_expired[S: StrEnum](status: S, expires_at: datetime, *, pending: S, expired: S) -> S:
    """Surface an overdue PENDING token as EXPIRED without writing to the DB.

    Shared by the invitation / email-verification / password-reset flows, whose
    status enums all carry distinct `pending` and `expired` members.
    """
    if status is pending and expires_at <= datetime.now(UTC):
        return expired
    return status
