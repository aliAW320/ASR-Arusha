from collections.abc import Iterable
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import History, Meeting, User, VoiceFile
from ..observability.context import get_log_context


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
    context = get_log_context()
    request_id = context.get("request_id")
    correlation_id = context.get("correlation_id")
    ip_address = context.get("client_ip")
    if request is not None:
        request_id = request_id or request.headers.get("x-request-id")
        ip_address = ip_address or (request.client.host if request.client else None)
    event = History(
        event_type=event_type,
        action_description=description,
        actor=actor,
        request_id=request_id,
        correlation_id=correlation_id,
        ip_address=ip_address,
        event_data=event_data,
        affected_users=list(affected_users),
        affected_meetings=list(affected_meetings),
        affected_voices=list(affected_voices),
    )
    session.add(event)
    return event
