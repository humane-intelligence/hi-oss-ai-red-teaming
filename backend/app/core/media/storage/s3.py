"""AWS S3 media storage — the cloud adapter behind the `MediaStorage` port.

Credentials come from the default boto3 chain (instance profile in prod, env vars locally),
mirroring app/core/exports/storage/s3.py; only the region, bucket, and key prefix are
configured. `key` is the bare object key — the prefix is re-applied on every access, so
switching backends never bakes a prefix into stored refs. An image is a single ≤20MB blob,
so `save` is one PUT (no multipart, unlike exports). Blocking boto calls run in a worker
thread (`anyio.to_thread`) so they never block the event loop.
"""

from collections.abc import Iterator

import boto3
from anyio import to_thread
from botocore.exceptions import ClientError

from app.core.media.storage.base import MediaStorage

_READ_CHUNK = 64 * 1024
# S3 signals "no such object" differently across head vs get; treat all as absent.
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class S3MediaStorage(MediaStorage):
    """Store/serve media blobs in an S3 bucket under an optional key prefix."""

    def __init__(self, *, bucket: str, aws_region: str, prefix: str) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._client = boto3.client("s3", region_name=aws_region)  # creds via the default chain

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def save(self, key: str, data: bytes, *, content_type: str) -> str:
        await to_thread.run_sync(
            lambda: self._client.put_object(
                Bucket=self._bucket, Key=self._key(key), Body=data, ContentType=content_type
            )
        )
        return key

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                return False
            raise
        return True

    def open(self, key: str) -> Iterator[bytes]:
        # `get_object` runs EAGERLY (not inside the returned generator) so a gone object raises
        # at call time — before the endpoint commits its 200 + headers — surfacing as a clean 404.
        # Honour the port contract: a missing object raises FileNotFoundError, not ClientError.
        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=self._key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                raise FileNotFoundError(key) from exc
            raise

        def _stream() -> Iterator[bytes]:
            body = obj["Body"]
            try:
                yield from body.iter_chunks(_READ_CHUNK)
            finally:
                # Release the pooled connection even on early close (client drops mid-download).
                body.close()

        return _stream()

    async def delete(self, key: str) -> None:
        # S3 delete is idempotent — deleting a missing key is a no-op, no error.
        await to_thread.run_sync(lambda: self._client.delete_object(Bucket=self._bucket, Key=self._key(key)))
