from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.providers import AuthenticationProvider, get_authentication_provider
from ...database import get_db_session
from ...dependencies import get_current_user
from ...models import User
from ...schemas import AuthResponse, LoginRequest, RegisterRequest, UserResponse
from ...security import create_access_token
from ...services.audit import add_history_event


router = APIRouter(prefix="/auth", tags=["auth"])


def _auth_response(user: User) -> AuthResponse:
    access_token, expires_in = create_access_token(str(user.id))
    return AuthResponse(access_token=access_token, expires_in=expires_in, user=user)


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register_user(
    payload: RegisterRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    provider: Annotated[AuthenticationProvider, Depends(get_authentication_provider)],
):
    email = str(payload.email).lower()
    if await session.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    user = User(email=email, full_name=payload.full_name)
    identity = provider.create_identity(user, email, payload.password)
    session.add_all([user, identity])
    add_history_event(
        session,
        event_type="auth.registered",
        description="User registered",
        actor=user,
        request=request,
        affected_users=[user],
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="A user with this email already exists") from None

    await session.refresh(user)
    return _auth_response(user)


@router.post("/login", response_model=AuthResponse)
async def login_user(
    payload: LoginRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    provider: Annotated[AuthenticationProvider, Depends(get_authentication_provider)],
):
    email = str(payload.email).lower()
    user = await provider.authenticate(session, email, payload.password)
    if user is None:
        affected_user = await session.scalar(select(User).where(User.email == email))
        add_history_event(
            session,
            event_type="auth.login_failed",
            description="Login failed",
            request=request,
            event_data={"email": email},
            affected_users=[affected_user] if affected_user else [],
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        add_history_event(
            session,
            event_type="auth.login_blocked",
            description="Inactive user attempted to log in",
            request=request,
            affected_users=[user],
        )
        await session.commit()
        raise HTTPException(status_code=403, detail="User account is inactive")

    add_history_event(
        session,
        event_type="auth.login_succeeded",
        description="User logged in",
        actor=user,
        request=request,
        affected_users=[user],
    )
    await session.commit()
    return _auth_response(user)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: Annotated[User, Depends(get_current_user)]):
    return current_user
