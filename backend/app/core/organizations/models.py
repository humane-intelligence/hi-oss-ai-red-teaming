"""Organization table — the tenancy root that scopes users and evaluation groups."""

from sqlalchemy import Index
from sqlalchemy import text
from sqlmodel import Field

from app.core.base_model import BaseModel


class Organization(BaseModel, table=True):
    __tablename__ = "organizations"
    __table_args__ = (
        # Partial unique index: a soft-deleted organization must not block
        # re-creating one with the same name. Uniqueness applies only to rows
        # where deleted_at IS NULL — mirrors the `users.email` / `roles.name` pattern.
        Index(
            "ix_organizations_name",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: str = Field(max_length=255, nullable=False)
    description: str | None = Field(default=None, max_length=1024)
