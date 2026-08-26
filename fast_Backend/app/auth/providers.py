from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ..models import AuthIdentity, User
from ..security import hash_password, verify_password


LOCAL_AUTH_PROVIDER = "local"


class AuthenticationProvider(Protocol):
    name: str

    async def authenticate(
        self,
        session: AsyncSession,
        subject: str,
        credential: str,
    ) -> User | None: ...

    def create_identity(self, user: User, subject: str, credential: str) -> AuthIdentity: ...


class LocalAuthenticationProvider:
    name = LOCAL_AUTH_PROVIDER

    async def authenticate(
        self,
        session: AsyncSession,
        subject: str,
        credential: str,
    ) -> User | None:
        identity = await session.scalar(
            select(AuthIdentity)
            .options(joinedload(AuthIdentity.user))
            .where(
                AuthIdentity.provider == self.name,
                AuthIdentity.subject == subject,
            )
        )
        if identity is None or identity.secret_hash is None:
            return None
        if not verify_password(credential, identity.secret_hash):
            return None
        return identity.user

    def create_identity(self, user: User, subject: str, credential: str) -> AuthIdentity:
        return AuthIdentity(
            user=user,
            provider=self.name,
            subject=subject,
            secret_hash=hash_password(credential),
        )


_local_provider = LocalAuthenticationProvider()


def get_authentication_provider() -> AuthenticationProvider:
    """Return today's provider; central providers can replace this dependency."""
    return _local_provider
