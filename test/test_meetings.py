import uuid

import pytest
from sqlalchemy import select

from app.models import MeetingMember, MeetingMemberRole, User, UserRole
from conftest import authorization, register_user


@pytest.mark.asyncio
async def test_owner_is_automatically_a_member_and_can_add_existing_user(client, session):
    owner = await register_user(client, "owner@example.com")
    member = await register_user(client, "member@example.com")
    created = await client.post(
        "/meetings",
        headers=authorization(owner),
        json={"title": "Architecture meeting"},
    )
    assert created.status_code == 201, created.text
    meeting_id = created.json()["id"]

    owner_membership = await session.get(
        MeetingMember,
        (uuid.UUID(meeting_id), uuid.UUID(owner["user"]["id"])),
    )
    assert owner_membership.role == MeetingMemberRole.OWNER

    added = await client.post(
        f"/meetings/{meeting_id}/members",
        headers=authorization(owner),
        json={"user_id": member["user"]["id"], "role": "contributor"},
    )
    assert added.status_code == 201, added.text
    assert added.json()["role"] == "contributor"


@pytest.mark.asyncio
async def test_contributor_can_edit_but_cannot_manage_members(client):
    owner = await register_user(client, "owner2@example.com")
    contributor = await register_user(client, "contributor@example.com")
    extra = await register_user(client, "extra@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Original"}
        )
    ).json()
    await client.post(
        f"/meetings/{meeting['id']}/members",
        headers=authorization(owner),
        json={"email": "contributor@example.com", "role": "contributor"},
    )

    updated = await client.patch(
        f"/meetings/{meeting['id']}",
        headers=authorization(contributor),
        json={"title": "Updated"},
    )
    denied = await client.post(
        f"/meetings/{meeting['id']}/members",
        headers=authorization(contributor),
        json={"user_id": extra["user"]["id"], "role": "viewer"},
    )
    assert updated.status_code == 200
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_view_and_update_every_meeting(client, session):
    owner = await register_user(client, "owner3@example.com")
    admin = await register_user(client, "admin@example.com")
    admin_user = await session.scalar(select(User).where(User.email == "admin@example.com"))
    admin_user.role = UserRole.ADMIN
    await session.commit()

    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Private"}
        )
    ).json()
    listed = await client.get("/meetings", headers=authorization(admin))
    updated = await client.patch(
        f"/meetings/{meeting['id']}",
        headers=authorization(admin),
        json={"description": "Admin edit"},
    )
    assert {item["id"] for item in listed.json()} == {meeting["id"]}
    assert updated.status_code == 200
    assert updated.json()["description"] == "Admin edit"
