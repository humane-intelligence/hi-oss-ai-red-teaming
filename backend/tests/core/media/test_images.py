"""Unit tests for the image processing service (validate + resize + EXIF orientation)."""

import base64
import io
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
import time_machine
from PIL import Image

from app.core.exceptions import BadRequestError
from app.core.media.services.images import cached_data_url
from app.core.media.services.images import clear_data_url_cache
from app.core.media.services.images import process_image
from app.core.media.services.images import read_data_url
from app.core.media.storage import get_media_storage


def _encode(width: int, height: int, fmt: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.mark.unit
def test_small_image_passes_without_resize() -> None:
    result = process_image(_encode(400, 300, "PNG"))

    assert (result.width, result.height) == (400, 300)
    assert result.ext == "png"
    assert result.content_type == "image/png"
    assert Image.open(io.BytesIO(result.data)).format == "PNG"


@pytest.mark.unit
def test_large_image_downscaled_preserving_aspect() -> None:
    result = process_image(_encode(3000, 1500, "JPEG"))

    assert (result.width, result.height) == (1200, 600)  # 2:1 aspect preserved, capped at 1200
    assert result.ext == "jpg"
    assert result.content_type == "image/jpeg"


@pytest.mark.unit
def test_png_alpha_channel_preserved() -> None:
    buffer = io.BytesIO()
    Image.new("RGBA", (50, 50), (255, 0, 0, 128)).save(buffer, format="PNG")

    result = process_image(buffer.getvalue())

    assert Image.open(io.BytesIO(result.data)).mode == "RGBA"


@pytest.mark.unit
def test_webp_roundtrip_preserves_format() -> None:
    result = process_image(_encode(300, 200, "WEBP"))

    assert result.ext == "webp"
    assert result.content_type == "image/webp"
    assert Image.open(io.BytesIO(result.data)).format == "WEBP"


@pytest.mark.unit
def test_mpo_normalized_to_jpeg() -> None:
    # A multi-picture JPEG (some phone cameras / 3D) sniffs as MPO; it must upload as a valid JPEG.
    buffer = io.BytesIO()
    Image.new("RGB", (300, 200), "red").save(
        buffer, format="MPO", save_all=True, append_images=[Image.new("RGB", (300, 200), "blue")]
    )

    result = process_image(buffer.getvalue())

    assert result.ext == "jpg"
    assert result.content_type == "image/jpeg"
    assert Image.open(io.BytesIO(result.data)).format == "JPEG"


@pytest.mark.unit
def test_rejects_unsupported_format() -> None:
    with pytest.raises(BadRequestError, match="Unsupported image format"):
        process_image(_encode(10, 10, "GIF"))


@pytest.mark.unit
def test_rejects_decompression_bomb(monkeypatch: pytest.MonkeyPatch) -> None:
    # A small file can decode to a huge bitmap; Pillow guards on MAX_IMAGE_PIXELS at open time.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)  # 400x300 = 120k px > 2x cap → bomb
    with pytest.raises(BadRequestError, match="not a readable image"):
        process_image(_encode(400, 300, "PNG"))


@pytest.mark.unit
def test_rejects_non_image() -> None:
    with pytest.raises(BadRequestError, match="not a readable image"):
        process_image(b"this is definitely not an image")


@pytest.mark.unit
def test_rejects_truncated_body() -> None:
    # Header stays valid (open + format sniff pass) but the scan data is cut off, so Pillow
    # only fails on decode — which happens outside the open() guard. Must be 400, not 500.
    full = _encode(800, 600, "JPEG")
    truncated = full[: len(full) // 2]
    assert Image.open(io.BytesIO(truncated)).format == "JPEG"  # open still succeeds
    with pytest.raises(BadRequestError, match="corrupt or truncated"):
        process_image(truncated)


@pytest.mark.unit
def test_exif_orientation_applied_before_resize() -> None:
    # Orientation 6 means the stored image must be rotated to display upright; exif_transpose
    # applies it, swapping a 100x200 into a 200x100 — so a phone photo isn't stored sideways.
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    Image.new("RGB", (100, 200), "green").save(buffer, format="JPEG", exif=exif)

    result = process_image(buffer.getvalue())

    assert (result.width, result.height) == (200, 100)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("key", "expected_prefix"),
    [
        ("2026/01/01/aaaa.png", "data:image/png;base64,"),
        ("2026/01/01/bbbb.jpg", "data:image/jpeg;base64,"),
        ("2026/01/01/cccc.bin", "data:application/octet-stream;base64,"),  # unknown ext falls back
    ],
)
async def test_read_data_url_round_trips_blob(key: str, expected_prefix: str) -> None:
    payload = b"\x89PNG-not-really-but-opaque-bytes"
    await get_media_storage().save(key, payload, content_type="application/octet-stream")

    url = await read_data_url(key)

    assert url.startswith(expected_prefix)
    assert base64.b64decode(url.split(",", 1)[1]) == payload


@pytest.mark.integration
async def test_cached_data_url_serves_from_memory_until_cleared() -> None:
    key = "2026/01/01/cache.png"
    await get_media_storage().save(key, b"opaque", content_type="application/octet-stream")
    first = await cached_data_url(key)
    await get_media_storage().delete(key)

    assert await cached_data_url(key) == first  # cache hit — no storage read

    clear_data_url_cache()
    with pytest.raises(FileNotFoundError):
        await cached_data_url(key)


@pytest.mark.integration
async def test_cached_data_url_expires_after_ttl() -> None:
    key = "2026/01/01/ttl.png"
    await get_media_storage().save(key, b"opaque", content_type="application/octet-stream")
    start = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)

    with time_machine.travel(start, tick=False) as traveller:
        await cached_data_url(key)
        await get_media_storage().delete(key)
        traveller.shift(timedelta(seconds=901))
        # Past the TTL the entry is dropped — a deleted blob can't be replayed indefinitely.
        with pytest.raises(FileNotFoundError):
            await cached_data_url(key)


@pytest.mark.integration
async def test_cached_data_url_evicts_oldest_when_over_byte_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each entry is ~55 bytes (23-byte png data-URL prefix + base64 of 24 bytes); a budget of
    # 80 holds exactly one, so caching the second evicts the first.
    monkeypatch.setattr("app.core.media.services.images._DATA_URL_CACHE.budget_bytes", 80)
    first, second = "2026/01/01/ev1.png", "2026/01/01/ev2.png"
    await get_media_storage().save(first, b"a" * 24, content_type="application/octet-stream")
    await get_media_storage().save(second, b"b" * 24, content_type="application/octet-stream")
    await cached_data_url(first)
    cached_second = await cached_data_url(second)
    await get_media_storage().delete(first)
    await get_media_storage().delete(second)

    assert await cached_data_url(second) == cached_second  # still cached
    with pytest.raises(FileNotFoundError):
        await cached_data_url(first)  # evicted, and its blob is gone
