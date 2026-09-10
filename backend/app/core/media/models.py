"""Media asset table — one row per uploaded image, tracking its storage key + metadata."""

import uuid

from sqlmodel import Field

from app.core.base_model import BaseModel


class MediaAsset(BaseModel, table=True):
    __tablename__ = "media_assets"

    # Opaque storage key (no backend prefix — re-applied by the adapter); also stored by the
    # referencing entity (e.g. Evaluation.cover_image). Unique by construction (embeds this row's id).
    key: str = Field(unique=True, index=True, max_length=1024)
    content_type: str = Field(max_length=128)
    size_bytes: int
    width: int
    height: int
    # Private assets serve only via signed URLs — the bare-key GET 404s them. Chosen at
    # upload, immutable, so a token minted for one mode can't be re-scoped by a later flip.
    is_private: bool = Field(default=False, sa_column_kwargs={"nullable": False, "server_default": "false"})
    # Uploader — the delete gate's owner check. No ondelete: users are soft-deleted, and a cover
    # must outlive its uploader's account (mirrors annotations.MessageFlag.created_by_id).
    created_by_id: uuid.UUID = Field(foreign_key="users.id", nullable=False, index=True)
