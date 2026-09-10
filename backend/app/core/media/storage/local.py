"""Local-filesystem media storage — the default backend for dev / single-box deploys.

Files live under `Settings.media_storage_dir`; `key` is a relative path re-resolved against
the base dir on every access, rejecting traversal escapes, so a tampered key can never read
outside the media dir. `save`/`delete` offload the blocking FS call to a worker thread
(`anyio.to_thread`), mirroring the S3 backend, so both stay loop-safe behind the shared
async signature (media runs on the app loop, not a single-coroutine worker like exports).
"""

from collections.abc import Iterator
from pathlib import Path

from anyio import to_thread

from app.core.media.storage.base import MediaStorage

_READ_CHUNK = 64 * 1024


class LocalMediaStorage(MediaStorage):
    """Write/read/delete media blobs on the local filesystem under `base_dir`."""

    def __init__(self, base_dir: str) -> None:
        self._base = Path(base_dir).resolve()

    def _resolve(self, key: str) -> Path:
        """Resolve `key` under the base dir, rejecting path-traversal escapes."""
        candidate = (self._base / key).resolve()
        if candidate != self._base and self._base not in candidate.parents:
            msg = f"key '{key}' escapes the media storage directory"
            raise ValueError(msg)
        return candidate

    async def save(self, key: str, data: bytes, *, content_type: str) -> str:
        path = self._resolve(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await to_thread.run_sync(_write)
        return key

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def open(self, key: str) -> Iterator[bytes]:
        # Acquire the handle EAGERLY (not inside the generator) so a gone file raises
        # FileNotFoundError at call time — before the endpoint commits its 200 + headers —
        # letting it become a clean 404 instead of a truncated body.
        handle = self._resolve(key).open("rb")

        def _stream() -> Iterator[bytes]:
            with handle:
                while chunk := handle.read(_READ_CHUNK):
                    yield chunk

        return _stream()

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        await to_thread.run_sync(lambda: path.unlink(missing_ok=True))
