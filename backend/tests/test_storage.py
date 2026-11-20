"""첨부 저장소 검증: 버킷·보관 기간 준비, 올리기/받기/지우기, 설정이 없을 때."""

import io

import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from app import main, storage
from app.config import Settings
from app.storage import LIFECYCLE_RULE_ID, ObjectNotFoundError, ObjectStorage, make_client

SETTINGS = Settings(
    s3_endpoint_url="http://minio:9000",
    s3_access_key_id="key",
    s3_secret_access_key="secret",
)

LIFECYCLE = {
    "Bucket": "attachments",
    "LifecycleConfiguration": {
        "Rules": [
            {
                "ID": LIFECYCLE_RULE_ID,
                "Status": "Enabled",
                "Filter": {"Prefix": ""},
                "Expiration": {"Days": 30},
            }
        ]
    },
}


@pytest.fixture
def stubbed():
    client = make_client(SETTINGS)
    with Stubber(client) as stub:
        yield ObjectStorage(client, "attachments", 30), stub
        stub.assert_no_pending_responses()


def test_client_uses_path_style_for_minio():
    client = make_client(SETTINGS)
    assert client.meta.endpoint_url == "http://minio:9000"
    assert client.meta.config.s3["addressing_style"] == "path"


async def test_ensure_bucket_creates_missing_bucket_and_sets_expiry(stubbed):
    s, stub = stubbed
    stub.add_client_error("head_bucket", "404", http_status_code=404)
    stub.add_response("create_bucket", {}, {"Bucket": "attachments"})
    stub.add_response("put_bucket_lifecycle_configuration", {}, LIFECYCLE)
    await s.ensure_bucket()


async def test_ensure_bucket_keeps_existing_bucket(stubbed):
    s, stub = stubbed
    stub.add_response("head_bucket", {}, {"Bucket": "attachments"})
    stub.add_response("put_bucket_lifecycle_configuration", {}, LIFECYCLE)
    await s.ensure_bucket()


async def test_ensure_bucket_tolerates_concurrent_creation(stubbed):
    s, stub = stubbed
    stub.add_client_error("head_bucket", "404", http_status_code=404)
    stub.add_client_error("create_bucket", "BucketAlreadyOwnedByYou", http_status_code=409)
    stub.add_response("put_bucket_lifecycle_configuration", {}, LIFECYCLE)
    await s.ensure_bucket()


async def test_ensure_bucket_raises_on_access_denied(stubbed):
    s, stub = stubbed
    stub.add_client_error("head_bucket", "403", http_status_code=403)
    with pytest.raises(Exception, match="403"):
        await s.ensure_bucket()


async def test_put_get_delete(stubbed):
    s, stub = stubbed
    stub.add_response(
        "put_object",
        {},
        {"Bucket": "attachments", "Key": "k", "Body": ANY, "ContentType": "application/pdf"},
    )
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(b"%PDF"), 4)},
        {"Bucket": "attachments", "Key": "k"},
    )
    stub.add_response("delete_object", {}, {"Bucket": "attachments", "Key": "k"})

    await s.put("k", b"%PDF", "application/pdf")
    assert await s.get("k") == b"%PDF"
    await s.delete("k")


async def test_get_missing_object(stubbed):
    s, stub = stubbed
    stub.add_client_error("get_object", "NoSuchKey", http_status_code=404)
    with pytest.raises(ObjectNotFoundError):
        await s.get("gone")


def test_retention_must_be_positive():
    with pytest.raises(ValueError):
        ObjectStorage(make_client(SETTINGS), "attachments", 0)


def test_storage_disabled_without_settings(monkeypatch):
    monkeypatch.setattr(storage, "get_settings", lambda: Settings(s3_endpoint_url=""))
    storage.get_storage.cache_clear()
    try:
        assert storage.get_storage() is None
    finally:
        storage.get_storage.cache_clear()


async def test_startup_continues_when_storage_is_down(monkeypatch, caplog):
    class Down:
        bucket, retention_days = "attachments", 30

        async def ensure_bucket(self):
            raise ConnectionError("minio is down")

    monkeypatch.setattr(main, "get_storage", lambda: Down())
    await main.prepare_storage()  # 예외가 밖으로 나오지 않는다
    assert "not available" in caplog.text
