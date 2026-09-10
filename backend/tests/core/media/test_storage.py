"""Unit tests for the media storage backends (local + S3) + the factory."""

import io
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from app.core.media.storage.local import LocalMediaStorage
from app.core.media.storage.s3 import S3MediaStorage


@pytest.mark.unit
async def test_local_storage_roundtrip(tmp_path: Path) -> None:
    storage = LocalMediaStorage(str(tmp_path))
    key = "2026/07/09/abc123.png"

    ref = await storage.save(key, b"\x89PNG\r\n", content_type="image/png")

    assert ref == key
    assert storage.exists(ref)
    assert b"".join(storage.open(ref)) == b"\x89PNG\r\n"
    await storage.delete(ref)
    assert not storage.exists(ref)
    await storage.delete(ref)  # idempotent — a second delete is a no-op


@pytest.mark.unit
async def test_local_storage_overwrites_existing_key(tmp_path: Path) -> None:
    storage = LocalMediaStorage(str(tmp_path))
    await storage.save("k.jpg", b"first", content_type="image/jpeg")
    await storage.save("k.jpg", b"second", content_type="image/jpeg")

    assert b"".join(storage.open("k.jpg")) == b"second"


@pytest.mark.unit
def test_local_storage_open_missing_raises(tmp_path: Path) -> None:
    storage = LocalMediaStorage(str(tmp_path))
    with pytest.raises(FileNotFoundError):
        storage.open("nope.png")


@pytest.mark.unit
def test_local_storage_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalMediaStorage(str(tmp_path))
    with pytest.raises(ValueError, match="escapes"):
        storage.exists("../evil.png")


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> tuple[S3MediaStorage, Stubber]:
    """An S3 backend whose boto client is stubbed — no network, no real creds."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    storage = S3MediaStorage(bucket="bkt", aws_region="us-east-1", prefix="images/")
    return storage, Stubber(storage._client)


@pytest.mark.unit
async def test_s3_save_puts_object_with_content_type(s3: tuple[S3MediaStorage, Stubber]) -> None:
    storage, stub = s3
    stub.add_response(
        "put_object",
        {},
        {"Bucket": "bkt", "Key": "images/2026/07/09/x.png", "Body": b"\x89PNG", "ContentType": "image/png"},
    )
    with stub:
        ref = await storage.save("2026/07/09/x.png", b"\x89PNG", content_type="image/png")
    assert ref == "2026/07/09/x.png"  # key is the bare ref (prefix re-applied on access)
    stub.assert_no_pending_responses()


@pytest.mark.unit
def test_s3_exists_true_and_false(s3: tuple[S3MediaStorage, Stubber]) -> None:
    storage, stub = s3
    stub.add_response("head_object", {}, {"Bucket": "bkt", "Key": "images/there.png"})
    stub.add_client_error("head_object", service_error_code="404")
    with stub:
        assert storage.exists("there.png") is True
        assert storage.exists("gone.png") is False


@pytest.mark.unit
def test_s3_exists_reraises_non_missing_error(s3: tuple[S3MediaStorage, Stubber]) -> None:
    storage, stub = s3
    stub.add_client_error("head_object", service_error_code="AccessDenied")
    with stub, pytest.raises(ClientError):
        storage.exists("locked.png")  # a non-404 error must propagate, not read as absent


@pytest.mark.unit
def test_s3_open_streams_bytes(s3: tuple[S3MediaStorage, Stubber]) -> None:
    storage, stub = s3
    data = b"\x89PNG\r\n\x1a\n"
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(data), len(data))},
        {"Bucket": "bkt", "Key": "images/x.png"},
    )
    with stub:
        assert b"".join(storage.open("x.png")) == data


@pytest.mark.unit
def test_s3_open_translates_missing_to_file_not_found(s3: tuple[S3MediaStorage, Stubber]) -> None:
    storage, stub = s3
    stub.add_client_error("get_object", service_error_code="NoSuchKey")
    with stub, pytest.raises(FileNotFoundError):
        list(storage.open("gone.png"))  # drive the generator so get_object runs


@pytest.mark.unit
def test_s3_open_reraises_non_missing_error(s3: tuple[S3MediaStorage, Stubber]) -> None:
    storage, stub = s3
    stub.add_client_error("get_object", service_error_code="AccessDenied")
    with stub, pytest.raises(ClientError):
        storage.open("locked.png")  # non-missing error must propagate, not become FileNotFoundError


@pytest.mark.unit
async def test_s3_delete(s3: tuple[S3MediaStorage, Stubber]) -> None:
    storage, stub = s3
    stub.add_response("delete_object", {}, {"Bucket": "bkt", "Key": "images/x.png"})
    with stub:
        await storage.delete("x.png")
    stub.assert_no_pending_responses()
