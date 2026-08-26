import pytest
from sqlalchemy import select

from app.models import User, UserRole
from conftest import authorization, register_user


@pytest.mark.asyncio
async def test_only_admin_can_view_history_and_the_view_is_audited(client, session):
    normal = await register_user(client, "history-user@example.com")
    admin = await register_user(client, "history-admin@example.com")
    admin_user = await session.scalar(
        select(User).where(User.email == "history-admin@example.com")
    )
    admin_user.role = UserRole.ADMIN
    await session.commit()

    denied = await client.get("/history", headers=authorization(normal))
    allowed = await client.get("/history", headers=authorization(admin))

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert any(item["event_type"] == "admin.history_viewed" for item in allowed.json())
    registration = next(
        item for item in allowed.json() if item["event_type"] == "auth.registered"
    )
    assert registration["affected_user_ids"]


@pytest.mark.asyncio
async def test_history_is_immutable_over_http(client, session):
    admin = await register_user(client, "immutable-admin@example.com")
    admin_user = await session.scalar(
        select(User).where(User.email == "immutable-admin@example.com")
    )
    admin_user.role = UserRole.ADMIN
    await session.commit()

    response = await client.delete(
        "/history/00000000-0000-0000-0000-000000000000",
        headers=authorization(admin),
    )
    assert response.status_code == 404
