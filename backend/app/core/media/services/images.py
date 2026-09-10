"""Image upload service: validate + resize an uploaded image, persist it as a MediaAsset."""

import base64
import io
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from anyio import to_thread
from PIL import Image
from PIL import ImageOps
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.exceptions import BadRequestError
from app.core.media.models import MediaAsset
from app.core.media.storage import get_media_storage

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_MAX_DIMENSION = 1200
# Pillow format name -> (file extension, content-type). Sniffed from content, never the request.
_ALLOWED_FORMATS: dict[str, tuple[str, str]] = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}
# Pillow sniffs a multi-picture JPEG (some phone cameras, 3D) as MPO — a JPEG container. Fold it
# to JPEG for both the allow-check and the re-encode, which keeps only the first frame.
_FORMAT_ALIASES: dict[str, str] = {"MPO": "JPEG"}
# Storage-key extension -> content-type (reverse of _ALLOWED_FORMATS), for building a data URL
# without a DB round-trip: the extension was set at upload from the sniffed format.
_EXT_CONTENT_TYPE: dict[str, str] = dict(_ALLOWED_FORMATS.values())


@dataclass(frozen=True)
class ProcessedImage:
    data: bytes
    ext: str
    content_type: str
    width: int
    height: int


def process_image(raw: bytes) -> ProcessedImage:
    """Sniff + validate the format, fix EXIF orientation, downscale to <=1200x1200, re-encode.

    Keeps the source format (PNG stays PNG with alpha, JPEG stays JPEG). CPU-bound (Pillow
    decode/encode) — call via `anyio.to_thread` from the async path. A file Pillow can't
    decode, or one whose format isn't allowed, raises `BadRequestError` (400).
    """
    try:
        image = Image.open(io.BytesIO(raw))
        image_format = _FORMAT_ALIASES.get(image.format, image.format) if image.format else None
    except Exception as exc:  # Pillow raises a grab-bag (UnidentifiedImageError, OSError, bomb, …)
        raise BadRequestError("Uploaded file is not a readable image.") from exc

    entry = _ALLOWED_FORMATS.get(image_format) if image_format else None
    if entry is None:
        allowed = ", ".join(sorted(_ALLOWED_FORMATS))
        raise BadRequestError(f"Unsupported image format {image_format or 'unknown'!r}; allowed: {allowed}.")
    ext, content_type = entry

    # Pillow decodes lazily: a truncated/corrupt body opens fine, then fails here on the first
    # pixel access (exif_transpose/thumbnail/save). Map that to 400, not an uncaught 500.
    try:
        image = ImageOps.exif_transpose(image)  # normalize orientation before any resize
        image.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION))  # shrinks only if larger; preserves aspect ratio
        buffer = io.BytesIO()
        image.save(buffer, format=image_format)
    except Exception as exc:
        raise BadRequestError("Uploaded image is corrupt or truncated.") from exc
    return ProcessedImage(buffer.getvalue(), ext, content_type, image.width, image.height)


def _build_key(asset_id: uuid.UUID, ext: str) -> str:
    """Timestamped, collision-free key: `{YYYY/MM/DD}/{id}.{ext}` (UTC), no storage prefix."""
    now = datetime.now(UTC)
    return f"{now:%Y/%m/%d}/{asset_id.hex}.{ext}"


async def create_image(
    session: AsyncSession,
    *,
    raw: bytes,
    created_by_id: uuid.UUID,
    is_private: bool = False,
) -> MediaAsset:
    """Validate/resize `raw`, store the blob, and add the tracking row (caller commits).

    The id is minted up front so the storage key maps 1:1 to the row. The blob is written
    before the row is added; a commit failure afterwards orphans the blob (best-effort GC is
    deferred — an orphaned blob is harmless).
    """
    processed = await to_thread.run_sync(process_image, raw)
    asset_id = uuid.uuid4()
    key = _build_key(asset_id, processed.ext)

    await get_media_storage().save(key, processed.data, content_type=processed.content_type)
    asset = MediaAsset(
        id=asset_id,
        key=key,
        content_type=processed.content_type,
        size_bytes=len(processed.data),
        width=processed.width,
        height=processed.height,
        created_by_id=created_by_id,
        is_private=is_private,
    )
    session.add(asset)
    return asset


async def get_assets_by_keys(session: AsyncSession, keys: Sequence[str]) -> dict[str, MediaAsset]:
    """Return the live `MediaAsset` rows for `keys`, keyed by storage key — one query for the batch."""
    if not keys:
        return {}
    statement = MediaAsset.live_select().where(col(MediaAsset.key).in_(keys))
    return {asset.key: asset for asset in (await session.execute(statement)).scalars().all()}


def _read_blob(key: str) -> bytes:
    return b"".join(get_media_storage().open(key))


async def read_data_url(key: str) -> str:
    """Read a stored image blob and encode it as a base64 `data:` URL.

    For feeding an image inline to a provider (multi-modal dispatch) without the
    provider having to fetch a URL. Content-type comes from the key extension
    (set at upload from the sniffed format). The storage read is sync — offloaded
    to a thread. Raises `FileNotFoundError` if the blob is gone.
    """
    ext = key.rsplit(".", 1)[-1].lower()
    content_type = _EXT_CONTENT_TYPE.get(ext, "application/octet-stream")
    data = await to_thread.run_sync(_read_blob, key)
    return f"data:{content_type};base64,{base64.b64encode(data).decode()}"


class _DataUrlCache:
    """Per-process, byte-bounded, expiring cache for `read_data_url` results.

    History rebuild re-reads and re-encodes every prior image on every turn — this caps
    that at ~one storage read per key per process. Bounded by payload BYTES, not entries
    (a 1200x1200 data URL can run ~2 MB). Entries expire so a blob deleted out-of-band
    (uploader delete, retention reaper) can't be replayed from memory indefinitely.
    """

    def __init__(self, *, budget_bytes: int, ttl_seconds: int) -> None:
        self.budget_bytes = budget_bytes
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[str, datetime]] = {}
        self._bytes = 0

    def get(self, key: str) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        data_url, expires_at = entry
        if datetime.now(UTC) >= expires_at:
            self.evict(key)
            return None
        return data_url

    def put(self, key: str, data_url: str) -> None:
        size = len(data_url)
        if size > self.budget_bytes:
            return
        self.evict(key)
        # Insertion-ordered dict → evicting the first key is FIFO; good enough for the
        # history-rebuild access pattern (every key re-read each turn).
        while self._bytes + size > self.budget_bytes and self._entries:
            self.evict(next(iter(self._entries)))
        self._entries[key] = (data_url, datetime.now(UTC) + timedelta(seconds=self.ttl_seconds))
        self._bytes += size

    def evict(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._bytes -= len(entry[0])

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0


_DATA_URL_CACHE = _DataUrlCache(budget_bytes=32 * 1024 * 1024, ttl_seconds=900)


def clear_data_url_cache() -> None:
    """Drop every cached data URL (test isolation; the suite runs in random order)."""
    _DATA_URL_CACHE.clear()


async def cached_data_url(key: str) -> str:
    """`read_data_url` behind a per-process, byte-bounded, expiring cache.

    Keys are immutable (they embed the asset id), so a fresh entry is always
    correct; the TTL only bounds how long a since-deleted blob can outlive its
    storage in memory. Consequence: the history-rebuild's vanished-blob → 404
    degradation is TTL-gated — a deleted or reaped blob keeps serving from cache
    (and re-sending to the provider) until its entry expires, not on delete.
    """
    cached = _DATA_URL_CACHE.get(key)
    if cached is not None:
        return cached
    data_url = await read_data_url(key)
    _DATA_URL_CACHE.put(key, data_url)
    return data_url
