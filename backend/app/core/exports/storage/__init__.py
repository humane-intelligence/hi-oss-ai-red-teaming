"""Export-file storage backends — shared port + concrete adapters + factory.

Public surface is re-exported here so callers stay on
`from app.core.exports.storage import X` regardless of which submodule the symbol
lives in. Adding a new backend (S3, Azure Blob) = one new file in this package +
one branch in `get_export_storage`, mirroring `app/core/email/backends/`.
"""

from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.core.exports.storage.base import ExportStorage
from app.core.exports.storage.local import LocalExportStorage
from app.core.exports.storage.s3 import S3ExportStorage

# Errors a storage backend may raise on a real (non-missing) delete/access failure:
# local FS `OSError`, a path-traversal `ValueError` from the local resolver, and S3's
# `ClientError` / `BotoCoreError`. Best-effort cleanup paths (the reaper sweep, the
# failed-job partial-file cleanup) catch exactly these — narrow enough not to swallow a
# programming error the way a bare `except Exception` would, while keeping the task layer
# from importing backend-specific exception types itself. Widen when a backend is added.
STORAGE_ERRORS: tuple[type[Exception], ...] = (OSError, ValueError, ClientError, BotoCoreError)

__all__ = [
    "STORAGE_ERRORS",
    "ExportStorage",
    "LocalExportStorage",
    "S3ExportStorage",
    "get_export_storage",
]


def get_export_storage() -> ExportStorage:
    """Build the backend named by `Settings.export_storage_backend` (fresh each call).

    Each provider's config coexists in `Settings`; the one `export_storage_backend`
    switch selects which adapter is built, and only that adapter's fields are read.
    Credentials themselves are resolved by the provider SDK (boto3's default chain for
    S3), never by this app — so switching backend is just flipping the one env var
    (given the provider's own creds are present). Required-field presence is enforced
    at startup by `Settings`, so a misconfigured backend fails fast, not here.
    """
    settings = get_settings()
    if settings.export_storage_backend == "local":
        return LocalExportStorage(settings.export_storage_dir)
    if settings.export_storage_backend == "s3":
        return S3ExportStorage(
            bucket=settings.export_s3_bucket,  # ty: ignore[invalid-argument-type]  # startup validator guarantees non-None
            aws_region=settings.aws_region,
            prefix=settings.export_s3_prefix,
        )
    msg = f"Export storage backend not implemented: {settings.export_storage_backend}"
    raise NotImplementedError(msg)
