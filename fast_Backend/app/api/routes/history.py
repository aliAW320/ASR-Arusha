import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...database import get_db_session
from ...dependencies import get_current_user
from ...models import History, User
from ...schemas import HistoryResponse
from ...services.audit import add_history_event
from ...services.permissions import is_admin


router = APIRouter(prefix="/history", tags=["history"])


def _history_response(event: History) -> HistoryResponse:
    return HistoryResponse(
        id=event.id,
        event_type=event.event_type,
        action_description=event.action_description,
        actor_user_id=event.actor_user_id,
        event_data=event.event_data,
        request_id=event.request_id,
        ip_address=event.ip_address,
        created_at=event.created_at,
        affected_user_ids=[item.id for item in event.affected_users],
        affected_meeting_ids=[item.id for item in event.affected_meetings],
        affected_voice_ids=[item.id for item in event.affected_voices],
        affected_result_artifact_ids=[item.id for item in event.affected_result_artifacts],
        affected_meeting_result_ids=[item.id for item in event.affected_meeting_results],
        affected_meeting_result_artifact_ids=[
            item.id for item in event.affected_meeting_result_artifacts
        ],
        affected_meeting_speaker_ids=[item.id for item in event.affected_meeting_speakers],
    )


@router.get("", response_model=list[HistoryResponse])
async def get_history(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    event_type: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    if not is_admin(current_user):
        raise HTTPException(status_code=403, detail="Administrator access required")

    add_history_event(
        session,
        event_type="admin.history_viewed",
        description="Administrator viewed audit history",
        actor=current_user,
        request=request,
        event_data={
            "event_type_filter": event_type,
            "actor_user_id_filter": str(actor_user_id) if actor_user_id else None,
            "offset": offset,
            "limit": limit,
        },
    )
    await session.commit()

    query = select(History).options(
        selectinload(History.affected_users),
        selectinload(History.affected_meetings),
        selectinload(History.affected_voices),
        selectinload(History.affected_result_artifacts),
        selectinload(History.affected_meeting_results),
        selectinload(History.affected_meeting_result_artifacts),
        selectinload(History.affected_meeting_speakers),
    )
    if event_type:
        query = query.where(History.event_type == event_type)
    if actor_user_id:
        query = query.where(History.actor_user_id == actor_user_id)
    events = (
        await session.scalars(
            query.order_by(History.created_at.desc()).offset(offset).limit(limit)
        )
    ).all()
    return [_history_response(event) for event in events]
