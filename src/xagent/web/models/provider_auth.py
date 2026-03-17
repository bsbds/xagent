from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

if TYPE_CHECKING:
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import Mapped

    Base = declarative_base()

    class UserProviderAuth(Base):  # type: ignore[valid-type, misc]
        """User-scoped provider credentials (OAuth tokens, etc.)."""

        id: Mapped[int]
        user_id: Mapped[int]
        provider_id: Mapped[str]
        access_token: Mapped[str | None]
        refresh_token: Mapped[str | None]
        expires_at: Mapped[datetime | None]
        account_id: Mapped[str | None]
        created_at: Mapped[datetime | None]
        updated_at: Mapped[datetime | None]
        user: Mapped[Any]

    class OAuthState(Base):  # type: ignore[valid-type, misc]
        """Short-lived OAuth state + PKCE verifier storage."""

        id: Mapped[int]
        user_id: Mapped[int]
        provider_id: Mapped[str]
        state: Mapped[str]
        code_verifier: Mapped[str]
        expires_at: Mapped[datetime]
        created_at: Mapped[datetime | None]
        user: Mapped[Any]
else:
    from .database import Base

    class UserProviderAuth(Base):  # type: ignore
        """User-scoped provider credentials (OAuth tokens, etc.)."""

        __tablename__ = "user_provider_auths"
        __table_args__ = (
            UniqueConstraint("user_id", "provider_id", name="uq_user_provider_auth"),
        )

        id = Column(Integer, primary_key=True, index=True)
        user_id = Column(
            Integer,
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
        provider_id = Column(String(100), nullable=False, index=True)

        access_token = Column(String(4096), nullable=True)
        refresh_token = Column(String(4096), nullable=True)
        expires_at = Column(DateTime(timezone=True), nullable=True)
        account_id = Column(String(255), nullable=True)

        created_at = Column(DateTime(timezone=True), server_default=func.now())
        updated_at = Column(DateTime(timezone=True), onupdate=func.now())

        user = relationship("User")

    class OAuthState(Base):  # type: ignore
        """Short-lived OAuth state + PKCE verifier storage."""

        __tablename__ = "oauth_states"
        __table_args__ = (UniqueConstraint("state", name="uq_oauth_state"),)

        id = Column(Integer, primary_key=True, index=True)
        user_id = Column(
            Integer,
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
        provider_id = Column(String(100), nullable=False, index=True)
        state = Column(String(512), nullable=False, index=True)
        code_verifier = Column(String(512), nullable=False)
        expires_at = Column(DateTime(timezone=True), nullable=False)

        created_at = Column(DateTime(timezone=True), server_default=func.now())

        user = relationship("User")
