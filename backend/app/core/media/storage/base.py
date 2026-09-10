"""Storage port for uploaded media blobs — the abstraction S3/other object stores plug into.

Byte-oriented, unlike the text-streaming export port (app/core/exports/storage/): an image
is a single ≤20MB blob, fully in memory after resize, so `save` takes bytes (one PUT) rather
than an async chunk stream. The concrete backend is selected by `Settings` in
[__init__.py](__init__.py), mirroring the export + email ports. `open` is sync — it feeds a
FastAPI `StreamingResponse`, iterated in a threadpool so a blocking read never stalls the loop.
"""

from abc import ABC
from abc import abstractmethod
from collections.abc import Iterator


class MediaStorage(ABC):
    """Persist and serve media blobs by an opaque `key`."""

    @abstractmethod
    async def save(self, key: str, data: bytes, *, content_type: str) -> str:
        """Persist `data` under `key` and return the opaque key.

        Overwrites any existing blob for `key`. `content_type` is stored as object metadata
        by backends that support it (S3); the local backend ignores it (the media-type is
        served from the DB row, not the blob).
        """

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Whether a blob is still stored — checked before streaming a download (else the endpoint 404s)."""

    @abstractmethod
    def open(self, key: str) -> Iterator[bytes]:
        """Yield the stored bytes for a streaming download, or raise `FileNotFoundError` if gone."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Drop the stored blob. A no-op if it is already gone (idempotent)."""
