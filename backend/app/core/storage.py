"""첨부 파일 저장소 (S3 호환 API).

로컬에서는 앱 전용 MinIO 를 쓰지만 MinIO 전용 API 는 쓰지 않는다. 주소와 키만 바꾸면
AWS S3, Cloudflare R2 등 다른 S3 호환 저장소로 옮길 수 있다.

boto3 는 동기 라이브러리라서 호출을 스레드로 넘겨 이벤트 루프를 막지 않는다.
"""

import logging
from functools import lru_cache

import anyio
import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

DOCUMENTS_PREFIX = "documents/"
"""보관 기간 규칙을 받는 객체 키 접두어 (대화 첨부 문서 원본)."""

PROJECTS_PREFIX = "projects/"
"""프로젝트 문서 원본의 객체 키 접두어. 보관 기간 규칙을 받지 않는다 (사용자가 지울 때까지 둔다)."""

LIFECYCLE_RULE_ID = "expire-attachments"
"""첨부 원본을 보관 기간 뒤에 지우는 버킷 lifecycle 규칙의 ID."""

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}


class ObjectNotFoundError(KeyError):
    """저장소에 없는 객체를 요청했다."""


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def make_client(settings: Settings) -> BaseClient:
    """설정으로 S3 클라이언트를 만든다.

    MinIO 는 버킷을 주소의 경로로 구분하므로(path-style) 그렇게 맞춘다.

    Args:
        settings: 앱 설정.

    Returns:
        boto3 S3 클라이언트.
    """
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=5,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


class ObjectStorage:
    """버킷 하나에 파일을 올리고, 받고, 지운다.

    Attributes:
        bucket: 사용하는 버킷 이름.
        retention_days: lifecycle 규칙으로 객체를 지우기까지의 기간(일).
    """

    def __init__(self, client: BaseClient, bucket: str, retention_days: int) -> None:
        """저장소를 만든다.

        Args:
            client: boto3 S3 클라이언트.
            bucket: 사용할 버킷 이름.
            retention_days: 보관 기간(일). 1 이상이어야 한다.

        Raises:
            ValueError: ``retention_days`` 가 1 보다 작을 때.
        """
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        self._client = client
        self.bucket = bucket
        self.retention_days = retention_days

    async def ensure_bucket(self) -> None:
        """버킷이 없으면 만들고, 보관 기간 lifecycle 규칙을 설정한다.

        앱 전용 버킷이라 lifecycle 설정을 통째로 덮어쓴다. 시작할 때마다 불러도 된다.

        Raises:
            botocore.exceptions.ClientError: 저장소가 요청을 거절했을 때.
            botocore.exceptions.EndpointConnectionError: 저장소에 연결할 수 없을 때.
        """
        await anyio.to_thread.run_sync(self._ensure_bucket_sync)

    def _ensure_bucket_sync(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            if _error_code(exc) not in _NOT_FOUND_CODES:
                raise
            self._create_bucket()
            logger.info("created attachment bucket %s", self.bucket)
        self._client.put_bucket_lifecycle_configuration(
            Bucket=self.bucket,
            LifecycleConfiguration={
                "Rules": [
                    {
                        "ID": LIFECYCLE_RULE_ID,
                        "Status": "Enabled",
                        # 문서 원본만 보관 기간 뒤에 지운다. 채팅 이미지(images/)는 대화 내용이라 대화와 함께 둔다.
                        "Filter": {"Prefix": DOCUMENTS_PREFIX},
                        "Expiration": {"Days": self.retention_days},
                    }
                ]
            },
        )

    def _create_bucket(self) -> None:
        region = self._client.meta.region_name
        kwargs: dict = {"Bucket": self.bucket}
        if region and region != "us-east-1":  # us-east-1 은 위치 지정을 받지 않는다
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            self._client.create_bucket(**kwargs)
        except ClientError as exc:
            # 다른 프로세스가 먼저 만든 경우
            if _error_code(exc) != "BucketAlreadyOwnedByYou":
                raise

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """객체를 올린다. 같은 키가 있으면 덮어쓴다.

        Args:
            key: 객체 키 (예: ``"documents/<대화 ID>/<문서 ID>"``).
            data: 파일 내용.
            content_type: MIME 타입. 원본을 다시 내려줄 때 쓴다.
        """
        await anyio.to_thread.run_sync(
            lambda: self._client.put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
            )
        )

    async def get(self, key: str) -> bytes:
        """객체 내용을 받는다.

        Args:
            key: 객체 키.

        Returns:
            파일 내용.

        Raises:
            ObjectNotFoundError: 객체가 없을 때 (보관 기간이 지나 지워진 경우 포함).
        """

        def read() -> bytes:
            try:
                res = self._client.get_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if _error_code(exc) in _NOT_FOUND_CODES:
                    raise ObjectNotFoundError(key) from exc
                raise
            with res["Body"] as body:
                return body.read()

        return await anyio.to_thread.run_sync(read)

    async def delete(self, key: str) -> None:
        """객체를 지운다. 없는 객체를 지워도 오류가 나지 않는다.

        Args:
            key: 객체 키.
        """
        await anyio.to_thread.run_sync(
            lambda: self._client.delete_object(Bucket=self.bucket, Key=key)
        )


@lru_cache
def get_storage() -> ObjectStorage | None:
    """설정에 맞는 첨부 저장소. 프로세스당 하나만 만든다.

    Returns:
        저장소. 설정이 비어 있으면(``Settings.storage_enabled`` 가 거짓) ``None``.
    """
    settings = get_settings()
    if not settings.storage_enabled:
        return None
    return ObjectStorage(
        make_client(settings), settings.s3_bucket, settings.attachment_retention_days
    )
