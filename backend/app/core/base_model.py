"""Abstract base for primary table models.

Importing this module sets SQLModel.metadata.naming_convention before any
table=True model is declared, so every constraint emitted by Alembic
autogenerate uses the same naming scheme.
"""

import uuid
from datetime import UTC
from datetime import datetime
from typing import Self
from typing import TypeVar

from sqlalchemy import DateTime
from sqlalchemy import Select
from sqlalchemy import Update
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlmodel import Field
from sqlmodel import SQLModel

from app.core.soft_delete import with_live

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Mutates in-place — replacing SQLModel.metadata would break SQLModel's
# internal registry, which holds its own reference to the same object.
SQLModel.metadata.naming_convention = NAMING_CONVENTION

_T = TypeVar("_T", bound="BaseModel")


class BaseModel(SQLModel):
    """Common fields and soft-delete factories for primary table models.

    All datetime columns are `timezone=True`; supply UTC-aware values.
    `live_*` classmethods filter tombstoned rows via `with_loader_criteria`;
    see [soft_delete.py](soft_delete.py) for multi-model propagation rules and
    [restore.py](restore.py) for reading tombstones back.

    Usage:
        class MyModel(BaseModel, table=True):
            __tablename__ = "my_models"
            name: str
    """

    # Use sa_type + sa_column_kwargs (not sa_column=Column(...)) so SQLModel
    # constructs a fresh Column per concrete subclass — a single Column instance
    # cannot be attached to more than one Table.
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": False, "server_default": func.now()},
    )
    updated_at: datetime = Field(
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={
            "nullable": False,
            "server_default": func.now(),
            "onupdate": func.now(),
        },
    )
    deleted_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # ty: ignore[invalid-argument-type]
        sa_column_kwargs={"nullable": True},
    )
    # Who tombstoned the row — scopes restore ("users restore only their own deletes").
    # No FK: the trail must survive the deleter's own tombstoning (same as AuditLog.actor_id).
    deleted_by_id: uuid.UUID | None = Field(default=None)

    def soft_delete(self, by_id: uuid.UUID | None) -> None:
        """Mark this row as soft-deleted by ``by_id`` (``None`` = system-initiated).

        Caller must flush/commit the session.
        """
        self.deleted_at = datetime.now(UTC)
        self.deleted_by_id = by_id

    def restore(self) -> None:
        """Clear the soft-delete flag. Caller must flush/commit the session."""
        self.deleted_at = None
        self.deleted_by_id = None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @staticmethod
    def live(instance: _T | None) -> _T | None:
        """Return ``instance`` unless it is absent or soft-deleted (then ``None``).

        The in-memory companion to `live_select`: guards a projection so a
        soft-deleted *related* row reads as no relation. Pair it with the row's
        own `from_*` projector at the call site, e.g.
        ``Schema.from_org(o) if (o := Organization.live(row.organization)) else None``.
        Needed because `with_loader_criteria` does not reliably filter a
        many-to-one relationship load across identity-map states.
        """
        return instance if instance is not None and instance.deleted_at is None else None

    @classmethod
    def live_select(cls) -> Select[tuple[Self]]:
        """`select(cls)` filtered to non-soft-deleted rows.

        Propagates to aliased uses of `cls` (eager-load joins, self-
        referential loads). Add `with_live(OtherModel)` to `.options(...)`
        to filter another soft-delete-aware model loaded in the same
        statement.
        """
        return select(cls).options(with_live(cls))

    @classmethod
    def live_update(cls) -> Update:
        """`update(cls)` filtered to live rows.

        `with_loader_criteria` renders as a WHERE on ORM UPDATEs, so bulk
        updates skip tombstoned rows without an explicit `.where(...)`.
        For bulk soft-delete, chain
        `.values(deleted_at=func.now(), deleted_by_id=actor_id)` so the
        timestamp is stamped server-side at execute time and the deleter is
        recorded for restore scoping (`app/core/restore.py`).
        """
        return update(cls).options(with_live(cls))
