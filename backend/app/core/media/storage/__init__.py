"""Media storage backends — shared port + concrete adapters + factory.

Adding a backend = one new file here + one branch in `get_media_storage`, mirroring
app/core/exports/storage/ and app/core/email/backends/.
"""

from app.core.config import get_settings
from app.core.media.storage.base import MediaStorage
from app.core.media.storage.local import LocalMediaStorage
from app.core.media.storage.s3 import S3MediaStorage

__all__ = [
    "LocalMediaStorage",
    "MediaStorage",
    "S3MediaStorage",
    "get_media_storage",
]


def get_media_storage() -> MediaStorage:
    """Build the backend named by `Settings.media_storage_backend` (fresh each call).

    Each provider's config coexists in `Settings`; the one switch selects which adapter is
    built. Required-field presence is enforced at startup by `Settings`, so a misconfigured
    backend fails fast there, not here.
    """
    settings = get_settings()
    if settings.media_storage_backend == "local":
        return LocalMediaStorage(settings.media_storage_dir)
    if settings.media_storage_backend == "s3":
        return S3MediaStorage(
            bucket=settings.media_s3_bucket,  # ty: ignore[invalid-argument-type]  # startup validator guarantees non-None
            aws_region=settings.aws_region,
            prefix=settings.media_s3_prefix,
        )
    msg = f"Media storage backend not implemented: {settings.media_storage_backend}"
    raise NotImplementedError(msg)
