from typing import BinaryIO, Protocol


class ObjectStorage(Protocol):
    async def put_object(
        self,
        bucket: str,
        object_key: str,
        data: BinaryIO,
        length: int,
        content_type: str,
    ) -> None: ...

    async def remove_object(self, bucket: str, object_key: str) -> None: ...
