import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"
os.environ["JWT_SECRET_KEY"] = "test-secret-key-that-is-at-least-32-characters"
os.environ["MINIO_MEETINGS_BUCKET"] = "test-meetings"

from app.database import Base, get_db_session  # noqa: E402
from app.main import app  # noqa: E402
from app.storage.minio import get_object_storage  # noqa: E402


class FakeObjectStorage:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.removed: list[tuple[str, str]] = []

    async def put_object(self, bucket, object_key, data, length, content_type):
        data.seek(0)
        content = data.read()
        assert len(content) == length
        self.objects[(bucket, object_key)] = content

    async def remove_object(self, bucket, object_key):
        self.removed.append((bucket, object_key))
        self.objects.pop((bucket, object_key), None)

    async def download_object(self, bucket, object_key, destination):
        destination.seek(0)
        destination.write(self.objects[(bucket, object_key)])
        destination.seek(0)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as database_session:
        yield database_session


@pytest_asyncio.fixture
async def client(session_factory):
    storage = FakeObjectStorage()

    async def override_session():
        async with session_factory() as database_session:
            yield database_session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_object_storage] = lambda: storage
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        http.storage = storage
        yield http
    app.dependency_overrides.clear()


async def register_user(client: AsyncClient, email: str, password: str = "password123"):
    response = await client.post(
        "/auth/register",
        json={"email": email, "password": password, "full_name": email.split("@")[0]},
    )
    assert response.status_code == 201, response.text
    return response.json()


def authorization(auth_response: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_response['access_token']}"}


async def mark_diarization_available(session_factory):
    """Pretend a diarization worker is alive, so uploads are offered the choice.

    Without a heartbeat the backend marks new voices `unavailable` and runs
    ASR -> cleaning with no speakers at all, so any test that wants the
    diarized pipeline has to declare that a worker exists.
    """
    from app.services.diarization import DIARIZATION_SERVICE, record_heartbeat

    async with session_factory() as session:
        await record_heartbeat(session, DIARIZATION_SERVICE)
        await session.commit()


async def accept_diarization(client, auth_response: dict, voice_id: str):
    """Answer "yes, split by speaker" for a voice, the way the user would."""
    response = await client.post(
        f"/voices/{voice_id}/diarization",
        headers=authorization(auth_response),
        json={"enabled": True},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def run_preprocessing(session_factory, storage, *, times: int = 1):
    """Run real preprocessing orchestration over tiny sentinel test audio."""
    from app.config import get_settings
    from preprocessing.audio import NormalizedAudio
    from preprocessing.worker import PreprocessingWorker

    async def copy_normalizer(source, destination):
        payload = source.read_bytes()
        destination.write_bytes(payload)
        return NormalizedAudio(duration_ms=1000, size_bytes=len(payload))

    worker = PreprocessingWorker(
        session_factory=session_factory,
        storage=storage,
        settings=get_settings(),
        normalizer=copy_normalizer,
    )
    for _ in range(times):
        assert await worker.run_once() is True
