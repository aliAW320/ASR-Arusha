import asyncio

from sqlalchemy import select

from ..auth.providers import LOCAL_AUTH_PROVIDER, LocalAuthenticationProvider
from ..config import get_settings
from ..database import SessionFactory, close_database
from ..models import AuthIdentity, User, UserRole
from ..security import hash_password
from ..services.audit import add_history_event


async def bootstrap_admin(session, email: str, password: str, full_name: str | None = None):
    email = email.lower()
    user = await session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, full_name=full_name, role=UserRole.ADMIN)
        identity = LocalAuthenticationProvider().create_identity(user, email, password)
        session.add_all([user, identity])
        action = "created"
    else:
        user.role = UserRole.ADMIN
        if full_name is not None:
            user.full_name = full_name
        identity = await session.scalar(
            select(AuthIdentity).where(
                AuthIdentity.user_id == user.id,
                AuthIdentity.provider == LOCAL_AUTH_PROVIDER,
            )
        )
        if identity is None:
            session.add(LocalAuthenticationProvider().create_identity(user, email, password))
        else:
            identity.subject = email
            identity.secret_hash = hash_password(password)
        action = "updated"

    add_history_event(
        session,
        event_type=f"admin.{action}",
        description=f"Bootstrap administrator {action}",
        actor=user,
        affected_users=[user],
    )
    await session.commit()
    return user, action


async def create_or_update_admin() -> None:
    settings = get_settings()
    if settings.admin_email is None or settings.admin_password is None:
        raise SystemExit("ADMIN_EMAIL and ADMIN_PASSWORD must be configured")

    email = str(settings.admin_email).lower()
    password = settings.admin_password.get_secret_value()
    if len(password) < 8:
        raise SystemExit("ADMIN_PASSWORD must contain at least 8 characters")

    async with SessionFactory() as session:
        _, action = await bootstrap_admin(
            session,
            email,
            password,
            settings.admin_full_name,
        )
        print(f"Administrator {email} {action}")


def main() -> None:
    async def run() -> None:
        try:
            await create_or_update_admin()
        finally:
            await close_database()

    asyncio.run(run())


if __name__ == "__main__":
    main()
