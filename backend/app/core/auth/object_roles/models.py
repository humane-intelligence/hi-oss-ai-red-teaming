"""The polymorphic per-object role assignment table.

One row per `(object_type, object_id, user, role)`. `object_id` carries no
DB-level foreign key — it points at a different table per `object_type` — so
referential integrity to the target object is the application's responsibility:
soft-delete the assignments when the object is removed. The global `roles`
catalog is reused by FK; what a role grants *at object scope* lives in
`registry.py`, not on the row.
"""

import uuid

from sqlalchemy import Enum
from sqlalchemy import Index
from sqlalchemy import text
from sqlmodel import Field

from app.core.auth.object_roles.registry import ObjectType
from app.core.base_model import BaseModel


class ObjectRoleAssignment(BaseModel, table=True):
    __tablename__ = "object_role_assignments"
    __table_args__ = (
        # Live rows are unique per (object, user, role); tombstoned rows are
        # excluded so a removed assignment can be re-added. The leading columns
        # also serve the per-(object, user) resolver lookup.
        Index(
            "ix_object_role_assignments_object_user_role",
            "object_type",
            "object_id",
            "user_id",
            "role_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Serves the per-(user, object_type) visibility subquery in
        # `app/core/evaluations/access.py` — which runs on every group listing and
        # can't use the unique index above (it leads with object_type/object_id) —
        # and the `user_id` FK delete cascade, which would otherwise be unindexed.
        Index(
            "ix_object_role_assignments_user_object_type",
            "user_id",
            "object_type",
        ),
    )

    object_type: ObjectType = Field(
        sa_type=Enum(  # ty: ignore[invalid-argument-type]
            ObjectType,
            values_callable=lambda enum: [m.value for m in enum],
            name="objecttype",
        ),
        sa_column_kwargs={"nullable": False},
    )
    object_id: uuid.UUID = Field(nullable=False)
    user_id: uuid.UUID = Field(
        foreign_key="users.id",
        nullable=False,
        ondelete="CASCADE",
    )
    # No ondelete: a role still assigned to any object must not be hard-deletable.
    role_id: uuid.UUID = Field(foreign_key="roles.id", nullable=False)
