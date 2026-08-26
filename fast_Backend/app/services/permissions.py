import enum

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Meeting, MeetingMember, MeetingMemberRole, User, UserRole
from ..observability.context import bind_log_context


class MeetingPermission(str, enum.Enum):
    VIEW = "view"
    EDIT = "edit"
    MANAGE_MEMBERS = "manage_members"
    MANAGE_VOICES = "manage_voices"
    DELETE = "delete"


# Kept in one policy map so future role changes do not leak into route handlers.
MEETING_ROLE_PERMISSIONS: dict[MeetingMemberRole, frozenset[MeetingPermission]] = {
    MeetingMemberRole.OWNER: frozenset(MeetingPermission),
    MeetingMemberRole.CONTRIBUTOR: frozenset(
        {
            MeetingPermission.VIEW,
            MeetingPermission.EDIT,
            MeetingPermission.MANAGE_VOICES,
        }
    ),
    MeetingMemberRole.VIEWER: frozenset({MeetingPermission.VIEW}),
}


def is_admin(user: User) -> bool:
    return user.role == UserRole.ADMIN


async def require_meeting_permission(
    session: AsyncSession,
    user: User,
    meeting_id,
    permission: MeetingPermission,
) -> Meeting:
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")

    bind_log_context(meeting_id=str(meeting.id))
    if is_admin(user):
        return meeting

    role = await session.scalar(
        select(MeetingMember.role).where(
            MeetingMember.meeting_id == meeting.id,
            MeetingMember.user_id == user.id,
        )
    )
    if role is None or permission not in MEETING_ROLE_PERMISSIONS[role]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permission")
    return meeting
