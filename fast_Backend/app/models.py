import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func , ForeignKey
from sqlalchemy.orm import Mapped, mapped_column , relationship

from .database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    meetings: Mapped[list["Meeting"]] = relationship(
    back_populates="owner",
    )

class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    meeting_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ),
        index=True,
        nullable=False,
    )
    owner: Mapped["User"] = relationship(back_populates="meetings")

