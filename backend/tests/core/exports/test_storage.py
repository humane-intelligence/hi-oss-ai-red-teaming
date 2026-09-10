"""Unit tests for the export storage backends (local + S3) and the job-create validator."""

import io
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from pydantic import ValidationError

from app.core.exports.schemas import ExportJobCreate
from app.core.exports.storage.local import LocalExportStorage
from app.core.exports.storage.s3 import S3ExportStorage


@pytest.mark.unit
async def test_local_storage_roundtrip(tmp_path: Path) -> None:
    storage = LocalExportStorage(str(tmp_path))

    async def _chunks():
        yield "a,b\n"
        yield "1,2\n"

    ref = await storage.save("job.csv", _chunks(), content_type="text/csv")

    assert storage.exists(ref)
    assert b"".join(storage.open(ref)) == b"a,b\n1,2\n"
    await storage.delete(ref)
    assert not storage.exists(ref)
    await storage.delete(ref)  # idempotent — a second delete is a no-op


@pytest.mark.unit
def test_local_storage_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalExportStorage(str(tmp_path))
    with pytest.raises(ValueError, match="escapes"):
        storage.exists("../evil.csv")


async def _achunks(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> tuple[S3ExportStorage, Stubber]:
    """An S3 backend whose boto client is stubbed — no network, no real creds."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    storage = S3ExportStorage(bucket="bkt", aws_region="us-east-1", prefix="exports/")
    return storage, Stubber(storage._client)


@pytest.mark.unit
async def test_s3_save_puts_object(s3: tuple[S3ExportStorage, Stubber]) -> None:
    storage, stub = s3
    stub.add_response(
        "put_object",
        {},
        {"Bucket": "bkt", "Key": "exports/job.csv", "Body": b"a,b\n1,2\n", "ContentType": "text/csv"},
    )
    with stub:
        ref = await storage.save("job.csv", _achunks("a,b\n", "1,2\n"), content_type="text/csv")
    assert ref == "job.csv"  # file_ref is the bare key (prefix re-applied on access)
    stub.assert_no_pending_responses()


@pytest.mark.unit
async def test_s3_save_tags_the_passed_content_type(s3: tuple[S3ExportStorage, Stubber]) -> None:
    # The caller's content_type tags the stored object's own metadata — NOT a hardcoded text/csv —
    # so a json export lands as application/json in S3 (direct access / lifecycle / presigned URLs).
    # The stub's expected_params fail the call if ContentType diverges (mutation-proof for the fix).
    storage, stub = s3
    stub.add_response(
        "put_object",
        {},
        {"Bucket": "bkt", "Key": "exports/job.json", "Body": b"[]", "ContentType": "application/json"},
    )
    with stub:
        await storage.save("job.json", _achunks("[]"), content_type="application/json")
    stub.assert_no_pending_responses()


@pytest.mark.unit
async def test_s3_save_large_switches_to_multipart(
    s3: tuple[S3ExportStorage, Stubber], monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, stub = s3
    monkeypatch.setattr("app.core.exports.storage.s3._PART_SIZE", 4)  # force multipart on tiny input
    stub.add_response(
        "create_multipart_upload",
        {"UploadId": "u1"},
        {"Bucket": "bkt", "Key": "exports/big.csv", "ContentType": "text/csv"},
    )
    stub.add_response(
        "upload_part",
        {"ETag": '"e1"'},
        {"Bucket": "bkt", "Key": "exports/big.csv", "UploadId": "u1", "PartNumber": 1, "Body": b"aaaa"},
    )
    stub.add_response(
        "upload_part",
        {"ETag": '"e2"'},
        {"Bucket": "bkt", "Key": "exports/big.csv", "UploadId": "u1", "PartNumber": 2, "Body": b"bb"},
    )
    stub.add_response(
        "complete_multipart_upload",
        {},
        {
            "Bucket": "bkt",
            "Key": "exports/big.csv",
            "UploadId": "u1",
            "MultipartUpload": {"Parts": [{"PartNumber": 1, "ETag": '"e1"'}, {"PartNumber": 2, "ETag": '"e2"'}]},
        },
    )
    with stub:
        ref = await storage.save("big.csv", _achunks("aa", "aa", "bb"), content_type="text/csv")  # 4 B flushes part 1
    assert ref == "big.csv"
    stub.assert_no_pending_responses()


@pytest.mark.unit
async def test_s3_save_aborts_multipart_on_error(
    s3: tuple[S3ExportStorage, Stubber], monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, stub = s3
    monkeypatch.setattr("app.core.exports.storage.s3._PART_SIZE", 4)
    stub.add_response(
        "create_multipart_upload",
        {"UploadId": "u1"},
        {"Bucket": "bkt", "Key": "exports/big.csv", "ContentType": "text/csv"},
    )
    stub.add_client_error("upload_part", service_error_code="InternalError")
    stub.add_response("abort_multipart_upload", {}, {"Bucket": "bkt", "Key": "exports/big.csv", "UploadId": "u1"})
    with stub, pytest.raises(ClientError):
        await storage.save(
            "big.csv", _achunks("aaaa"), content_type="text/csv"
        )  # part flush → upload_part errors → abort
    stub.assert_no_pending_responses()  # abort was called (no orphaned multipart)


@pytest.mark.unit
def test_s3_exists_true_and_false(s3: tuple[S3ExportStorage, Stubber]) -> None:
    storage, stub = s3
    stub.add_response("head_object", {}, {"Bucket": "bkt", "Key": "exports/there.csv"})
    stub.add_client_error("head_object", service_error_code="404")
    with stub:
        assert storage.exists("there.csv") is True
        assert storage.exists("gone.csv") is False


@pytest.mark.unit
def test_s3_open_streams_bytes(s3: tuple[S3ExportStorage, Stubber]) -> None:
    storage, stub = s3
    data = b"a,b\n1,2\n"
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(data), len(data))},
        {"Bucket": "bkt", "Key": "exports/x.csv"},
    )
    with stub:
        assert b"".join(storage.open("x.csv")) == data


@pytest.mark.unit
def test_s3_open_translates_missing_to_file_not_found(s3: tuple[S3ExportStorage, Stubber]) -> None:
    # Port contract: a gone object raises FileNotFoundError (not a botocore ClientError), so the
    # download's straight-to-open() path surfaces a since-reaped file as a clean 404.
    storage, stub = s3
    stub.add_client_error("get_object", service_error_code="NoSuchKey")
    with stub, pytest.raises(FileNotFoundError):
        list(storage.open("gone.csv"))  # drive the generator so get_object runs


@pytest.mark.unit
async def test_s3_delete(s3: tuple[S3ExportStorage, Stubber]) -> None:
    storage, stub = s3
    stub.add_response("delete_object", {}, {"Bucket": "bkt", "Key": "exports/x.csv"})
    with stub:
        await storage.delete("x.csv")
    stub.assert_no_pending_responses()


@pytest.mark.unit
def test_export_job_create_requires_exactly_one_scope() -> None:
    ExportJobCreate(template="flags", evaluation_id=uuid4())  # single scope → ok
    ExportJobCreate(template="flags", evaluation_group_id=uuid4())  # single scope → ok
    with pytest.raises(ValidationError):
        ExportJobCreate(template="flags")  # no scope
    with pytest.raises(ValidationError):
        ExportJobCreate(template="flags", evaluation_id=uuid4(), evaluation_group_id=uuid4())  # both scopes
