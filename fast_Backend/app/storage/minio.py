from functools import lru_cache
import shutil
from typing import BinaryIO

from anyio import to_thread
from minio import Minio
from minio.error import S3Error

from ..config import get_settings


class MinioObjectStorage:
    def __init__(self, client: Minio):
        self.client = client

    def _ensure_bucket(self, bucket: str) -> None:
        if self.client.bucket_exists(bucket):
            return
        try:
            self.client.make_bucket(bucket)
        except S3Error as error:
            if error.code not in {"BucketAlreadyExists", "BucketAlreadyOwnedByYou"}:
                raise

    async def put_object(
        self,
        bucket: str,
        object_key: str,
        data: BinaryIO,
        length: int,
        content_type: str,
    ) -> None:
        def upload() -> None:
            self._ensure_bucket(bucket)
            data.seek(0)
            self.client.put_object(
                bucket,
                object_key,
                data,
                length,
                content_type=content_type,
            )

        await to_thread.run_sync(upload)

    async def remove_object(self, bucket: str, object_key: str) -> None:
        await to_thread.run_sync(self.client.remove_object, bucket, object_key)

    async def download_object(
        self,
        bucket: str,
        object_key: str,
        destination: BinaryIO,
    ) -> None:
        def download() -> None:
            response = self.client.get_object(bucket, object_key)
            try:
                destination.seek(0)
                shutil.copyfileobj(response, destination)
                destination.seek(0)
            finally:
                response.close()
                response.release_conn()

        await to_thread.run_sync(download)


@lru_cache
def get_object_storage() -> MinioObjectStorage:
    settings = get_settings()
    return MinioObjectStorage(
        Minio(
            settings.minio_endpoint,
            access_key=settings.minio_root_user,
            secret_key=settings.minio_root_password.get_secret_value(),
            secure=settings.minio_secure,
        )
    )
