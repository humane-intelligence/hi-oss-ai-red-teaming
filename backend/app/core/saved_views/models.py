"""Saved-views table — a user's named filter/sort/column state for a list view.

A `SavedView` persists the state of one list view (`resource`) under a `name`,
owned by the user who created it. The `state` blob is **opaque** to the backend:
the frontend owns its shape (filters, `order_by`, column visibility) and
re-issues the normal list request on recall. So there is no per-endpoint
coupling — this single table serves every savable list.
"""

import uuid
from typing import Any

from sqlalchemy import Index
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from app.core.base_model import BaseModel


class SavedView(BaseModel, table=True):
    __tablename__ = "saved_views"
    __table_args__ = (
        # One live view name per (owner, resource); the partial index survives
        # soft-delete and serves the owner-scoped `created_by_id [+ resource]` reads.
        Index(
            "ix_saved_views_created_by_id_resource_name",
            "created_by_id",
            "resource",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    created_by_id: uuid.UUID = Field(foreign_key="users.id", nullable=False)
    # Client-facing list key (see `SavedViewResource`); a plain string column so a
    # new savable list is an enum member, not a migration. Validated at the edge.
    resource: str = Field(max_length=64, nullable=False)
    name: str = Field(max_length=128, nullable=False)
    # Opaque client-owned view state: filters, `order_by`, column visibility.
    state: dict[str, Any] = Field(
        default_factory=dict,
        sa_type=JSONB,
        sa_column_kwargs={"nullable": False, "server_default": text("'{}'::jsonb")},
    )
