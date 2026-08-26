import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db_session
from ...dependencies import get_current_user
from ...models import (
    Meeting,
    MeetingMember,
    MeetingMemberRole,
    User,
)
from ...schemas import CreateMeetingRequest , MeetingResponse

router = APIRouter(prefix="/meetings", tags=["meetings"])

@router.post("/create" , status_code=status.HTTP_201_CREATED)
async def create_meeting(
    payload: CreateMeetingRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    new_meeting = Meeting(
        title=payload.title,
        description=payload.description,
        meeting_date=payload.date,
        owner_id=current_user.id,
    )
    session.add(new_meeting)
    await session.commit()
    await session.refresh(new_meeting)

    return MeetingResponse(
        id=new_meeting.id,
        title=new_meeting.title,
        description=new_meeting.description,
        date=new_meeting.meeting_date,
    )
    

@router.post("/process")
async def process_meeting():
    pass


@router.get("/list")
async def list_meetings(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    query = (
        select(Meeting)
        .join(MeetingMember, Meeting.id == MeetingMember.meeting_id)
        .where((MeetingMember.user_id == current_user.id) | (Meeting.owner_id == current_user.id))
    )
    result = await session.execute(query)
    meetings = result.scalars().all()

    return [
        MeetingResponse(
            id=meeting.id,
            title=meeting.title,
            description=meeting.description,
            date=meeting.meeting_date,
        )
        for meeting in meetings
    ]
    


@router.get("/{meeting_id}")
async def get_meeting(meeting_id: str):
    pass


@router.get("/{meeting_id}/status")
async def get_meeting_status(meeting_id: str):
    pass


@router.delete("/{meeting_id}")
async def delete_meeting(meeting_id: str):
    pass
