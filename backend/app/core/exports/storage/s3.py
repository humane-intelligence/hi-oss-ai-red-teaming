"""AWS S3 export storage — the cloud adapter behind the `ExportStorage` port.

Credentials come from the default boto3 chain (instance profile in prod, env vars
locally), mirroring [app/core/email/backends/ses.py](../../email/backends/ses.py);
only the region, bucket, and key prefix are configured. `file_ref` is the bare
object key — the prefix is re-applied on every access, matching how the local
backend treats a bare filename, so switching backends never bakes a prefix into
stored refs.
"""

import contextlib
from collections.abc import AsyncIterator
from collections.abc import Iterator

import boto3
from anyio import to_thread
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError

from app.core.exports.storage.base import ExportStorage

_READ_CHUNK = 64 * 1024
# Buffer this much before flushing a multipart part. S3 requires ≥5 MiB per part (except the
# last); 8 MiB keeps peak memory flat regardless of total export size.
_PART_SIZE = 8 * 1024 * 1024
# S3 signals "no such object" differently across head vs get; treat all as absent.
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


class S3ExportStorage(ExportStorage):
    """Store/serve export blobs in an S3 bucket under an optional key prefix."""

    def __init__(self, *, bucket: str, aws_region: str, prefix: str) -> None:
        self._bucket = bucket
        self._prefix = prefix
        self._client = boto3.client("s3", region_name=aws_region)  # creds via the default chain

    def _key(self, file_ref: str) -> str:
        return f"{self._prefix}{file_ref}"

    async def _upload_part(self, full_key: str, upload_id: str, number: int, body: bytes) -> dict[str, object]:
        result = await to_thread.run_sync(
            lambda: self._client.upload_part(
                Bucket=self._bucket, Key=full_key, UploadId=upload_id, PartNumber=number, Body=body
            )
        )
        return {"PartNumber": number, "ETag": result["ETag"]}

    async def save(self, key: str, chunks: AsyncIterator[str], content_type: str) -> str:
        # Stream to S3 with a bounded buffer: small exports go in one PUT; larger ones switch
        # to a multipart upload once the buffer crosses _PART_SIZE, so peak memory is ~one part
        # regardless of total size. Each blocking boto call runs in a worker thread
        # (`anyio.to_thread`) so it never blocks the event loop.
        full_key = self._key(key)
        buffer = bytearray()
        upload_id: str | None = None
        parts: list[dict[str, object]] = []
        try:
            async for chunk in chunks:
                buffer.extend(chunk.encode("utf-8"))
                if len(buffer) >= _PART_SIZE:
                    if upload_id is None:
                        created = await to_thread.run_sync(
                            lambda: self._client.create_multipart_upload(
                                Bucket=self._bucket, Key=full_key, ContentType=content_type
                            )
                        )
                        upload_id = created["UploadId"]
                    parts.append(await self._upload_part(full_key, upload_id, len(parts) + 1, bytes(buffer)))
                    buffer = bytearray()
            if upload_id is None:
                # Stayed under one part — a single PUT avoids multipart overhead.
                body = bytes(buffer)
                await to_thread.run_sync(
                    lambda: self._client.put_object(
                        Bucket=self._bucket, Key=full_key, Body=body, ContentType=content_type
                    )
                )
            else:
                if buffer:  # trailing part (the last part may be < 5 MiB)
                    parts.append(await self._upload_part(full_key, upload_id, len(parts) + 1, bytes(buffer)))
                completed_id = upload_id
                await to_thread.run_sync(
                    lambda: self._client.complete_multipart_upload(
                        Bucket=self._bucket, Key=full_key, UploadId=completed_id, MultipartUpload={"Parts": parts}
                    )
                )
        except Exception:
            # Catch broadly here on purpose: the failure may come from a boto call OR from the
            # chunk producer (`async for chunk`), and a multipart upload must be aborted on ANY
            # of them — then the original error is re-raised (never swallowed).
            if upload_id is not None:
                aborted_id = upload_id
                # The abort is best-effort cleanup, so suppress its own boto errors (incl.
                # connection-level BotoCoreError) so they can't mask the original failure.
                with contextlib.suppress(ClientError, BotoCoreError):
                    await to_thread.run_sync(
                        lambda: self._client.abort_multipart_upload(
                            Bucket=self._bucket, Key=full_key, UploadId=aborted_id
                        )
                    )
            raise
        return key

    def exists(self, file_ref: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._key(file_ref))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                return False
            raise
        return True

    def open(self, file_ref: str) -> Iterator[bytes]:
        # `get_object` runs EAGERLY (not inside the returned generator) so a gone object raises
        # at call time — before the download endpoint commits its 200 + headers — surfacing as a
        # clean 404 rather than a truncated body. Honour the port contract: a missing object
        # raises FileNotFoundError, not a botocore ClientError.
        try:
            obj = self._client.get_object(Bucket=self._bucket, Key=self._key(file_ref))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_CODES:
                raise FileNotFoundError(file_ref) from exc
            raise

        def _stream() -> Iterator[bytes]:
            body = obj["Body"]
            try:
                yield from body.iter_chunks(_READ_CHUNK)
            finally:
                # Release the pooled connection even on early close (e.g. a client that drops
                # mid-download) — without this the StreamingBody leaks and the pool exhausts.
                # The download path drains fully, but don't rely on the caller finishing.
                body.close()

        return _stream()

    async def delete(self, file_ref: str) -> None:
        # S3 delete is idempotent — deleting a missing key is a no-op, no error.
        await to_thread.run_sync(lambda: self._client.delete_object(Bucket=self._bucket, Key=self._key(file_ref)))
