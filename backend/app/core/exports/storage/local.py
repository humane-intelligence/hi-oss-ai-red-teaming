"""Local-filesystem export storage — the default backend for dev / single-box deploys.

Files live under `Settings.export_storage_dir`, one per job keyed by `{job_id}.{csv,json}`.
`file_ref` is just that relative filename; every access re-resolves it against the
base directory and rejects anything that escapes it, so a tampered `file_ref` can
never read outside the export dir.
"""

from collections.abc import AsyncIterator
from collections.abc import Iterator
from pathlib import Path

from app.core.exports.storage.base import ExportStorage

_READ_CHUNK = 64 * 1024


class LocalExportStorage(ExportStorage):
    """Write/read/delete export blobs on the local filesystem under `base_dir`."""

    def __init__(self, base_dir: str) -> None:
        self._base = Path(base_dir).resolve()

    def _resolve(self, file_ref: str) -> Path:
        """Resolve `file_ref` under the base dir, rejecting path-traversal escapes."""
        candidate = (self._base / file_ref).resolve()
        if candidate != self._base and self._base not in candidate.parents:
            msg = f"file_ref '{file_ref}' escapes the export storage directory"
            raise ValueError(msg)
        return candidate

    async def save(self, key: str, chunks: AsyncIterator[str], content_type: str) -> str:
        # `content_type` is part of the port contract but has no home on a plain filesystem blob
        # (the extension in `key` already encodes the format); it matters only for object stores (S3).
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Blocking write is acceptable: the worker runs one export generation per
        # event loop (via asyncio.run), so there are no peer coroutines to starve.
        with path.open("wb") as handle:
            async for chunk in chunks:
                handle.write(chunk.encode("utf-8"))
        return key

    def exists(self, file_ref: str) -> bool:
        return self._resolve(file_ref).is_file()

    def open(self, file_ref: str) -> Iterator[bytes]:
        # Acquire the handle EAGERLY (not inside the generator) so a gone file raises
        # FileNotFoundError at call time — before the download endpoint commits its 200 +
        # headers — letting it become a clean 404 instead of a truncated body.
        handle = self._resolve(file_ref).open("rb")

        def _stream() -> Iterator[bytes]:
            with handle:
                while data := handle.read(_READ_CHUNK):
                    yield data

        return _stream()

    async def delete(self, file_ref: str) -> None:
        self._resolve(file_ref).unlink(missing_ok=True)
