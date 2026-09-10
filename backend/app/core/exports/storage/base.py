"""Storage port for finished export files — the abstraction S3/Azure plug into later.

The concrete backend (local filesystem today) is selected by `Settings` in
[__init__.py](__init__.py), mirroring the email-backend port. Keeping the surface
tiny — write a keyed blob, stream it back, drop it — is what lets a cloud object
store slot in without touching the job/task/endpoint code above it.

`save` consumes the export text stream the worker produces — CSV or JSON, tagged
by the `content_type` the caller passes (async — the worker runs its generation in
an event loop). `open` is sync: it feeds a FastAPI
`StreamingResponse`, which iterates a sync generator in a threadpool, so a
blocking read never stalls the download handler's event loop.
"""

from abc import ABC
from abc import abstractmethod
from collections.abc import AsyncIterator
from collections.abc import Iterator


class ExportStorage(ABC):
    """Persist and serve generated export files by an opaque `file_ref`."""

    @abstractmethod
    async def save(self, key: str, chunks: AsyncIterator[str], content_type: str) -> str:
        """Persist the export text `chunks` under `key`; return the opaque `file_ref`.

        `content_type` tags the stored blob's own metadata (CSV or JSON) — so a backend whose
        objects carry a media type (S3) records the right one for direct access / lifecycle rules /
        presigned URLs. Overwrites any existing blob for `key` so a redelivered (at-least-once)
        task re-run is idempotent.
        """

    @abstractmethod
    def exists(self, file_ref: str) -> bool:
        """Whether a blob is still stored — checked before streaming a download (else the endpoint 404s)."""

    @abstractmethod
    def open(self, file_ref: str) -> Iterator[bytes]:
        """Yield the stored bytes for a streaming download, or raise `FileNotFoundError` if gone."""

    @abstractmethod
    async def delete(self, file_ref: str) -> None:
        """Drop the stored blob. A no-op if it is already gone (idempotent for the reaper)."""
