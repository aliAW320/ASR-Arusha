import pytest
from sqlalchemy import func, select

from app.models import AuthIdentity, History, User, UserRole
from app.cli.create_admin import bootstrap_admin
from conftest import authorization, register_user


@pytest.mark.asyncio
async def test_register_returns_token_user_and_local_identity(client, session):
    auth = await register_user(client, "new@example.com")

    assert auth["token_type"] == "bearer"
    assert auth["access_token"]
    assert auth["user"]["email"] == "new@example.com"
    assert auth["user"]["role"] == UserRole.USER.value

    assert await session.scalar(select(func.count(AuthIdentity.id))) == 1
    assert await session.scalar(
        select(func.count(History.id)).where(History.event_type == "auth.registered")
    ) == 1


@pytest.mark.asyncio
async def test_login_returns_same_auth_contract_and_me_works(client):
    registered = await register_user(client, "login@example.com")
    response = await client.post(
        "/auth/login",
        json={"email": "login@example.com", "password": "password123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["user"]["id"] == registered["user"]["id"]
    me = await client.get("/auth/me", headers=authorization(body))
    assert me.status_code == 200
    assert me.json()["email"] == "login@example.com"


@pytest.mark.asyncio
async def test_failed_login_is_audited_without_authenticating(client, session):
    await register_user(client, "failed@example.com")
    response = await client.post(
        "/auth/login",
        json={"email": "failed@example.com", "password": "wrong-password"},
    )

    assert response.status_code == 401
    event = await session.scalar(
        select(History).where(History.event_type == "auth.login_failed")
    )
    assert event is not None
    assert event.actor_user_id is None
    assert event.event_data["email"] == "failed@example.com"


@pytest.mark.asyncio
async def test_admin_bootstrap_is_idempotent_and_updates_local_password(session):
    first, first_action = await bootstrap_admin(
        session, "root@example.com", "first-password", "Root"
    )
    second, second_action = await bootstrap_admin(
        session, "root@example.com", "second-password", "Administrator"
    )

    assert first.id == second.id
    assert first_action == "created"
    assert second_action == "updated"
    assert second.role == UserRole.ADMIN
    assert second.full_name == "Administrator"
    assert await session.scalar(select(func.count(User.id))) == 1
