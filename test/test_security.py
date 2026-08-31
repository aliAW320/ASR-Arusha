import io

import pytest
from sqlalchemy import select

from app.models import History, User, UserRole
from conftest import authorization, register_user


@pytest.mark.asyncio
async def test_protected_routes_reject_missing_malformed_and_tampered_tokens(client):
    registered = await register_user(client, "token-check@example.com")
    token = registered["access_token"]
    tampered = f"{token[:-1]}{'a' if token[-1] != 'a' else 'b'}"

    for headers in (
        {},
        {"Authorization": "Basic ignored"},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": f"Bearer {tampered}"},
    ):
        response = await client.get("/auth/me", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_inactive_user_cannot_use_an_existing_token_or_login(client, session):
    registered = await register_user(client, "inactive@example.com")
    user = await session.scalar(select(User).where(User.email == "inactive@example.com"))
    user.is_active = False
    await session.commit()

    token_response = await client.get("/auth/me", headers=authorization(registered))
    login_response = await client.post(
        "/auth/login",
        json={"email": "inactive@example.com", "password": "password123"},
    )

    assert token_response.status_code == 401
    assert login_response.status_code == 403


@pytest.mark.asyncio
async def test_auth_requests_reject_unexpected_fields_including_role_escalation(client):
    register_response = await client.post(
        "/auth/register",
        json={
            "email": "untrusted-role@example.com",
            "password": "password123",
            "role": "admin",
        },
    )
    login_response = await client.post(
        "/auth/login",
        json={"email": "untrusted-role@example.com", "password": "password123", "role": "admin"},
    )

    assert register_response.status_code == 422
    assert login_response.status_code == 422


@pytest.mark.asyncio
async def test_auth_responses_and_audit_events_never_expose_passwords(client, session):
    password = "not-for-response-or-history"
    registered = await client.post(
        "/auth/register",
        json={"email": "secret-check@example.com", "password": password},
    )
    failed_login = await client.post(
        "/auth/login",
        json={"email": "secret-check@example.com", "password": "incorrect-password"},
    )
    events = (await session.scalars(select(History))).all()

    assert registered.status_code == 201
    assert password not in registered.text
    assert failed_login.status_code == 401
    assert all(password not in str(event.event_data) for event in events)
    assert all("incorrect-password" not in str(event.event_data) for event in events)


@pytest.mark.asyncio
async def test_non_member_cannot_read_or_upload_to_a_private_meeting(client):
    owner = await register_user(client, "private-owner@example.com")
    outsider = await register_user(client, "private-outsider@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Private"}
        )
    ).json()

    details = await client.get(f"/meetings/{meeting['id']}", headers=authorization(outsider))
    members = await client.get(
        f"/meetings/{meeting['id']}/members", headers=authorization(outsider)
    )
    voices = await client.get(
        f"/meetings/{meeting['id']}/voices", headers=authorization(outsider)
    )
    upload = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(outsider),
        files={"upload": ("blocked.wav", io.BytesIO(b"audio"), "audio/wav")},
    )

    assert [response.status_code for response in (details, members, voices, upload)] == [403] * 4
    assert client.storage.objects == {}


@pytest.mark.asyncio
async def test_viewer_cannot_upload_or_manage_other_members(client):
    owner = await register_user(client, "viewer-owner@example.com")
    viewer = await register_user(client, "viewer@example.com")
    target = await register_user(client, "viewer-target@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Roles"}
        )
    ).json()
    added = await client.post(
        f"/meetings/{meeting['id']}/members",
        headers=authorization(owner),
        json={"user_id": viewer["user"]["id"], "role": "viewer"},
    )
    assert added.status_code == 201

    upload = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(viewer),
        files={"upload": ("blocked.wav", b"audio", "audio/wav")},
    )
    manage_members = await client.post(
        f"/meetings/{meeting['id']}/members",
        headers=authorization(viewer),
        json={"user_id": target["user"]["id"], "role": "viewer"},
    )

    assert upload.status_code == 403
    assert manage_members.status_code == 403
    assert client.storage.objects == {}


@pytest.mark.asyncio
async def test_owner_membership_cannot_be_reassigned_or_removed(client):
    owner = await register_user(client, "protected-owner@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Owner integrity"}
        )
    ).json()

    change_role = await client.patch(
        f"/meetings/{meeting['id']}/members/{owner['user']['id']}",
        headers=authorization(owner),
        json={"role": "viewer"},
    )
    remove_owner = await client.delete(
        f"/meetings/{meeting['id']}/members/{owner['user']['id']}",
        headers=authorization(owner),
    )

    assert change_role.status_code == 409
    assert remove_owner.status_code == 409


@pytest.mark.asyncio
async def test_global_voice_inventory_and_audit_pagination_are_protected(client, session):
    normal_user = await register_user(client, "restricted-inventory@example.com")
    admin = await register_user(client, "security-admin@example.com")
    admin_user = await session.scalar(select(User).where(User.email == "security-admin@example.com"))
    admin_user.role = UserRole.ADMIN
    await session.commit()

    inventory = await client.get("/voices", headers=authorization(normal_user))
    audit = await client.get("/history", headers=authorization(normal_user))
    oversized_page = await client.get(
        "/history", params={"limit": 201}, headers=authorization(admin)
    )

    assert inventory.status_code == 403
    assert audit.status_code == 403
    assert oversized_page.status_code == 422


@pytest.mark.asyncio
async def test_uploaded_object_key_cannot_contain_client_filename_path_segments(client):
    owner = await register_user(client, "safe-filename@example.com")
    meeting = (
        await client.post(
            "/meetings", headers=authorization(owner), json={"title": "Filename"}
        )
    ).json()

    response = await client.post(
        f"/meetings/{meeting['id']}/voices",
        headers=authorization(owner),
        files={"upload": ("../../private file.wav", b"audio", "audio/wav")},
    )

    assert response.status_code == 201
    key = response.json()["minio_key"]
    assert ".." not in key
    assert "private_file.wav" in key
