"""Media API schemas."""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.core.media.models import MediaAsset


class MediaAssetResponse(BaseModel):
    """An uploaded image: its opaque storage `key`, the URL to fetch it, and metadata."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "a1b2c3d4-1111-2222-3333-444455556666",
                "key": "2026/07/09/a1b2c3d4111122223333444455556666.png",
                "url": "/api/v1/images/2026/07/09/a1b2c3d4111122223333444455556666.png",
                "content_type": "image/png",
                "width": 1200,
                "height": 800,
                "size_bytes": 154213,
                "is_private": False,
                "created_at": "2026-07-09T12:00:00Z",
            }
        }
    )

    id: UUID = Field(description="Server-assigned asset id.")
    key: str = Field(description="Opaque storage key; store this on the referencing entity (e.g. cover_image).")
    url: str = Field(description="Path to fetch the image (`GET /api/v1/images/{key}`).")
    content_type: str = Field(description="MIME type of the stored image.")
    width: int = Field(description="Width in pixels after any resize.")
    height: int = Field(description="Height in pixels after any resize.")
    size_bytes: int = Field(description="Stored (post-resize) byte size.")
    is_private: bool = Field(
        description="Private assets serve only via signed URLs; the public bare-key GET 404s them."
    )
    created_at: datetime = Field(description="Upload timestamp (UTC).")

    @classmethod
    def from_asset(cls, asset: MediaAsset) -> Self:
        return cls(
            id=asset.id,
            key=asset.key,
            url=f"/api/v1/images/{asset.key}",
            content_type=asset.content_type,
            width=asset.width,
            height=asset.height,
            size_bytes=asset.size_bytes,
            is_private=asset.is_private,
            created_at=asset.created_at,
        )


class SignedUrlResponse(BaseModel):
    """A time-limited signed URL to fetch an image, plus when it stops working."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "url": "/api/v1/images/signed/ImIwLzA3LzA5L3gucG5nIg.aG1234.abcDEF...",
                "expires_at": "2026-07-09T12:15:00Z",
            }
        }
    )

    url: str = Field(description="Signed path to fetch the image; rejected (403) once expired.")
    expires_at: datetime = Field(description="UTC instant after which the URL no longer works.")
