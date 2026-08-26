from collections.abc import Iterable
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import History, Meeting, User, VoiceFile


def add_history_event(
    session: AsyncSession,
    *,
    event_type: str,
    description: str,
    actor: User | None = None,
    request: Request | None = None,
    event_data: dict[str, Any] | None = None,
    affected_users: Iterable[User] = (),
    affected_meetings: Iterable[Meeting] = (),
    affected_voices: Iterable[VoiceFile] = (),
) -> History:
    request_id = request.headers.get("x-request-id") if request else None
    ip_address = request.client.host if request and request.client else None
    event = History(
        event_type=event_type,
        action_description=description,
        actor=actor,
        request_id=request_id,
        ip_address=ip_address,
        event_data=event_data,
        affected_users=list(affected_users),
        affected_meetings=list(affected_meetings),
        affected_voices=list(affected_voices),
    )
    session.add(event)
    return event
