"""Versioned Terms-of-Service documents — the text a user's consent points at.

A row is immutable once written (there is no update endpoint): rewriting the text of a
version users already accepted would silently change what they agreed to. Correcting one
means publishing the next version. The *current* document is the one with the newest
`published_at` — derived rather than flagged, so there is no "which one is current" to drift.
"""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Index
from sqlalchemy import Text
from sqlalchemy import text
from sqlmodel import Field

from app.core.base_model import BaseModel


class TermsDocument(BaseModel, table=True):
    __tablename__ = "terms_documents"
    __table_args__ = (
        Index(
            "ix_terms_documents_version",
            "version",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    version: str = Field(max_length=64, nullable=False)
    content: str = Field(sa_type=Text, sa_column_kwargs={"nullable": False})
    # Which version is current is domain state, so it is stamped from the application clock
    # rather than read off `created_at`: that column defaults to Postgres `now()`, which is
    # transaction-start time, and two documents written in one transaction would tie there
    # with nothing to break the tie by (the same wrinkle `User.invitations` orders around).
    published_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": False, "index": True},
    )
