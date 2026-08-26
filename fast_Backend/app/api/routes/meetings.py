import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ...database import get_db_session
from ...dependencies import get_current_user
from ...models import Meeting, MeetingMember, MeetingMemberRole, User
from ...observability.context import bind_log_context
from ...schemas import (
    AddMeetingMemberRequest,
    CreateMeetingRequest,
    MeetingMemberResponse,
    MeetingResponse,
    UpdateMeetingMemberRequest,
    UpdateMeetingRequest,
)
from ...services.audit import add_history_event
from ...services.permissions import (
    MeetingPermission,
    is_admin,
    require_meeting_permission,
)


router = APIRouter(prefix="/meetings", tags=["meetings"])


def _meeting_response(meeting: Meeting) -> MeetingResponse:
    return MeetingResponse(
        id=meeting.id,
        title=meeting.title,
        description=meeting.description,
        date=meeting.meeting_date,
        owner_id=meeting.owner_id,
        created_at=meeting.created_at,
        updated_at=meeting.updated_at,
    )


@router.post("", response_model=MeetingResponse, status_code=status.HTTP_201_CREATED)
@router.post("/create", response_model=MeetingResponse, status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def create_meeting(
    payload: CreateMeetingRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    meeting = Meeting(
        title=payload.title,
        description=payload.description,
        meeting_date=payload.date,
        owner=current_user,
    )
    membership = MeetingMember(
        meeting=meeting,
        user=current_user,
        role=MeetingMemberRole.OWNER,
    )
    session.add_all([meeting, membership])
    add_history_event(
        session,
        event_type="meeting.created",
        description="Meeting created",
        actor=current_user,
        request=request,
        affected_meetings=[meeting],
    )
    await session.commit()
    await session.refresh(meeting)
    bind_log_context(meeting_id=str(meeting.id))
    return _meeting_response(meeting)


@router.get("", response_model=list[MeetingResponse])
@router.get("/list", response_model=list[MeetingResponse], include_in_schema=False)
async def list_meetings(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    query = select(Meeting)
    if not is_admin(current_user):
        query = query.where(
            or_(
                Meeting.owner_id == current_user.id,
                Meeting.memberships.any(MeetingMember.user_id == current_user.id),
            )
        )
    meetings = (await session.scalars(query.order_by(Meeting.created_at.desc()))).all()
    return [_meeting_response(meeting) for meeting in meetings]


@router.get("/{meeting_id}", response_model=MeetingResponse)
async def get_meeting(
    meeting_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.VIEW
    )
    return _meeting_response(meeting)


@router.patch("/{meeting_id}", response_model=MeetingResponse)
async def update_meeting(
    meeting_id: uuid.UUID,
    payload: UpdateMeetingRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.EDIT
    )
    changes = payload.model_dump(exclude_unset=True)
    if "date" in changes:
        changes["meeting_date"] = changes.pop("date")
    for field, value in changes.items():
        setattr(meeting, field, value)
    add_history_event(
        session,
        event_type="meeting.updated",
        description="Meeting updated",
        actor=current_user,
        request=request,
        event_data={"fields": sorted(changes)},
        affected_meetings=[meeting],
    )
    await session.commit()
    await session.refresh(meeting)
    return _meeting_response(meeting)


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(
    meeting_id: uuid.UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.DELETE
    )
    add_history_event(
        session,
        event_type="meeting.deleted",
        description="Meeting deleted",
        actor=current_user,
        request=request,
        event_data={"meeting_id": str(meeting.id), "title": meeting.title},
    )
    await session.delete(meeting)
    await session.commit()


@router.get("/{meeting_id}/members", response_model=list[MeetingMemberResponse])
async def list_members(
    meeting_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    await require_meeting_permission(session, current_user, meeting_id, MeetingPermission.VIEW)
    memberships = (
        await session.scalars(
            select(MeetingMember)
            .options(joinedload(MeetingMember.user))
            .where(MeetingMember.meeting_id == meeting_id)
            .order_by(MeetingMember.added_at)
        )
    ).all()
    return memberships


@router.post(
    "/{meeting_id}/members",
    response_model=MeetingMemberResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    meeting_id: uuid.UUID,
    payload: AddMeetingMemberRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.MANAGE_MEMBERS
    )
    target = (
        await session.get(User, payload.user_id)
        if payload.user_id
        else await session.scalar(select(User).where(User.email == str(payload.email).lower()))
    )
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    membership = MeetingMember(meeting=meeting, user=target, role=payload.role)
    session.add(membership)
    add_history_event(
        session,
        event_type="meeting.member_added",
        description="Meeting member added",
        actor=current_user,
        request=request,
        event_data={"role": payload.role.value},
        affected_users=[target],
        affected_meetings=[meeting],
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="User is already a meeting member") from None
    await session.refresh(membership)
    await session.refresh(membership, attribute_names=["user"])
    return membership


@router.patch("/{meeting_id}/members/{user_id}", response_model=MeetingMemberResponse)
async def update_member(
    meeting_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: UpdateMeetingMemberRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.MANAGE_MEMBERS
    )
    membership = await session.scalar(
        select(MeetingMember)
        .options(joinedload(MeetingMember.user))
        .where(MeetingMember.meeting_id == meeting_id, MeetingMember.user_id == user_id)
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Meeting member not found")
    if membership.role == MeetingMemberRole.OWNER or user_id == meeting.owner_id:
        raise HTTPException(status_code=409, detail="Owner membership cannot be changed")
    membership.role = payload.role
    add_history_event(
        session,
        event_type="meeting.member_role_changed",
        description="Meeting member role changed",
        actor=current_user,
        request=request,
        event_data={"role": payload.role.value},
        affected_users=[membership.user],
        affected_meetings=[meeting],
    )
    await session.commit()
    await session.refresh(membership)
    return membership


@router.delete("/{meeting_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    meeting_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
):
    meeting = await require_meeting_permission(
        session, current_user, meeting_id, MeetingPermission.MANAGE_MEMBERS
    )
    membership = await session.get(MeetingMember, (meeting_id, user_id))
    if membership is None:
        raise HTTPException(status_code=404, detail="Meeting member not found")
    if membership.role == MeetingMemberRole.OWNER or user_id == meeting.owner_id:
        raise HTTPException(status_code=409, detail="Owner membership cannot be removed")
    target = await session.get(User, user_id)
    add_history_event(
        session,
        event_type="meeting.member_removed",
        description="Meeting member removed",
        actor=current_user,
        request=request,
        event_data={"user_id": str(user_id)},
        affected_users=[target] if target else [],
        affected_meetings=[meeting],
    )
    await session.delete(membership)
    await session.commit()
