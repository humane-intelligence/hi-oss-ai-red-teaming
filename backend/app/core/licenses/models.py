"""Data-license rows — the curated catalog's table plus user-authored licenses.

The platform-default singleton (`PlatformSettings`) lives in `app.core.platform_settings`.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Index
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy import text
from sqlalchemy.orm import column_property
from sqlalchemy.orm import defer
from sqlalchemy.orm.strategy_options import _AbstractLoad
from sqlmodel import Field

from app.core.base_model import BaseModel


class DataLicense(BaseModel, table=True):
    """A data license the platform offers.

    Curated licenses carry `created_by_id IS NULL` and are reconciled from `catalog.py` by
    `sync_licenses`; all but the `No license` entry also carry a `spdx_id`, and the only field the
    API may write on them is `content`, where the catalog ships none (see
    `licenses/service.py`). User-authored licenses have `created_by_id` set (their author) and never
    a `spdx_id`; an owner may edit only their own. `content` is the full legal text;
    `short_description` the one-line gist.
    Soft-deletable — a tombstoned license disappears from the picker but still resolves for the
    groups/evaluations that already reference it (license lineage doesn't lapse).
    """

    __tablename__ = "data_licenses"
    # `spdx_id` is unique among live rows only, so a curated id can be re-added after a tombstone.
    __table_args__ = (
        Index(
            "ix_data_licenses_spdx_id",
            "spdx_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: str = Field(max_length=255, nullable=False)
    version: str | None = Field(default=None, max_length=64)
    short_description: str = Field(sa_type=Text, sa_column_kwargs={"nullable": False})
    content: str = Field(sa_type=Text, sa_column_kwargs={"nullable": False})
    reference_url: str | None = Field(default=None, max_length=1024)
    # Licences that forbid redistributing raw conversations: message text of the conversations written
    # under one is sealed at rest. Export authority is unaffected — it stays the owner/admin gate.
    protects_conversation_data: bool = Field(
        default=False,
        sa_column_kwargs={"nullable": False, "server_default": text("false")},
    )
    # Set for the curated set (null for user-authored); the deterministic `curated_license_id`.
    spdx_id: str | None = Field(default=None, max_length=64)
    # Author of a user-authored license; NULL marks a curated/system license (managed in code).
    # SET NULL on hard user delete — the row survives its author (users are normally soft-deleted).
    created_by_id: uuid.UUID | None = Field(default=None, foreign_key="users.id", ondelete="SET NULL", index=True)

    if TYPE_CHECKING:
        # Mapped below as a `column_property`, which a type checker cannot see. Declared as a
        # property (not an annotated attribute) so it does not read as a constructor field; the
        # block never runs, so SQLModel sees nothing here either.
        @property
        def has_text(self) -> bool: ...


def defer_license_text() -> _AbstractLoad:
    """Loader option that leaves `content` behind, and refuses a later read of it.

    Every path that projects a licence answers `has_content` from the mapped `has_text` flag, so none
    of them needs the body — and the body is unbounded free text (a real CC licence runs to ~18 KB,
    and this branch is what makes admins fill those columns in). `raiseload` rather than a plain
    `defer` so a future consumer of `lic.content` on such a row fails loudly instead of lazy-loading
    one query per row — which under async surfaces as `MissingGreenlet`, far from the cause.

    Every reader of the text goes through `get_data_license` / `get_restorable_data_license`, neither
    of which applies this: the four routes returning `DataLicenseResponse` (detail, create, update,
    restore) and the update route's `content_before` capture for the audit trail (the snapshot itself
    leaves the body out).

    Typed `_AbstractLoad` rather than the public `ExecutableOption`: the chained call sites pass the
    result to `Load.options(...)`, which accepts only the narrower type.
    """
    return defer(DataLicense.content, raiseload=True)  # ty: ignore[invalid-argument-type]


# Whether the row carries a text, computed in SQL so a projection can answer it without transferring
# the legal text — which runs to tens of KB and is read on every list that resolves an effective
# licence. Mapped after the class body on purpose: SQLModel reads annotated class attributes as
# columns, and this is a mapper-level expression, not a column (nothing for Alembic to see). Unloaded
# on a just-inserted row, so the write paths refresh it explicitly (see `licenses/service.py`).
DataLicense.has_text = column_property(func.length(DataLicense.content) > 0)  # ty: ignore[invalid-assignment]
