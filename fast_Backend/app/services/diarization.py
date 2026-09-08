"""Liveness of the optional diarization worker.

Diarization is the only stage that lives in its own image (Pyannote/torch) and
may not be deployed at all, so "can we diarize right now?" is a runtime
question, not a config constant. The worker writes a heartbeat while it is
consuming; the API treats a heartbeat older than the TTL as gone.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..models import DiarizationDecision, ServiceHeartbeat, VoiceFile
from ..observability.logging import get_logger


logger = get_logger(__name__)


DIARIZATION_SERVICE = "diarization-worker"


async def record_heartbeat(session: AsyncSession, service_name: str) -> ServiceHeartbeat:
    now = datetime.now(timezone.utc)
    heartbeat = await session.get(ServiceHeartbeat, service_name)
    if heartbeat is None:
        heartbeat = ServiceHeartbeat(service_name=service_name, last_seen_at=now)
        session.add(heartbeat)
    else:
        heartbeat.last_seen_at = now
    return heartbeat


async def last_seen_at(session: AsyncSession, service_name: str) -> datetime | None:
    return await session.scalar(
        select(ServiceHeartbeat.last_seen_at).where(
            ServiceHeartbeat.service_name == service_name
        )
    )


async def diarization_is_available(session: AsyncSession, settings: Settings) -> bool:
    if not settings.diarization_enabled:
        return False
    seen_at = await last_seen_at(session, DIARIZATION_SERVICE)
    if seen_at is None:
        return False
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=timezone.utc)
    deadline = datetime.now(timezone.utc) - timedelta(
        seconds=settings.diarization_heartbeat_ttl_seconds
    )
    return seen_at >= deadline


async def resolve_decision(
    session: AsyncSession,
    voice: VoiceFile,
    settings: Settings,
) -> DiarizationDecision:
    """Decide what a new run for this voice should do about diarization.

    An answer the user already gave (enabled/skipped) survives reprocessing --
    they should not be asked the same question twice. Anything else is
    re-evaluated, so a voice uploaded while the worker was down can still be
    offered diarization once it comes back.
    """
    if voice.diarization_decision in {
        DiarizationDecision.ENABLED,
        DiarizationDecision.SKIPPED,
    }:
        return voice.diarization_decision
    if await diarization_is_available(session, settings):
        return DiarizationDecision.PENDING
    return DiarizationDecision.UNAVAILABLE


async def heartbeat_forever(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    service_name: str = DIARIZATION_SERVICE,
) -> None:
    """Report this worker alive until cancelled."""
    while True:
        try:
            async with session_factory() as session:
                await record_heartbeat(session, service_name)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A missed heartbeat only makes the API offer diarization less;
            # it must never take the worker itself down.
            logger.warning(
                "service_heartbeat_failed",
                "Could not record the diarization worker heartbeat",
                error=str(error),
                service=service_name,
            )
        await asyncio.sleep(settings.diarization_heartbeat_interval_seconds)
