"""SQLAlchemy ORM models – sensitive fields are stored encrypted (LargeBinary).

Tables:
- users            : accounts with hashed passwords (PBKDF2-HMAC-SHA256)
- sessions         : chat sessions per user
- messages         : encrypted chat history (role + content ciphertext)
- security_events  : encrypted security/abuse log entries
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, create_engine
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from ..config import settings
from .crypto import crypto


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------- password hashing (never reversible) ----------
_PBKDF2_ITERATIONS = 390_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return secrets.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_index: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # blind index
    email_enc: Mapped[bytes] = mapped_column(LargeBinary)                           # ciphertext
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sessions: Mapped[list["ChatSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    # transparent accessors -------------------------------------------------
    @property
    def email(self) -> str:
        return crypto.decrypt(self.email_enc)

    @email.setter
    def email(self, value: str) -> None:
        self.email_index = crypto.blind_index(value.lower().strip())
        self.email_enc = crypto.encrypt(value)

    def set_password(self, raw: str) -> None:
        self.password_hash = hash_password(raw)

    def check_password(self, raw: str) -> bool:
        return verify_password(raw, self.password_hash)


class ChatSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, default=lambda: secrets.token_urlsafe(12))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title_enc: Mapped[bytes] = mapped_column(LargeBinary, default=lambda: crypto.encrypt("New chat"))
    model: Mapped[str] = mapped_column(String(64), default="auto")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="sessions")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Message.seq"
    )

    @property
    def title(self) -> str:
        return crypto.decrypt(self.title_enc)

    @title.setter
    def title(self, value: str) -> None:
        self.title_enc = crypto.encrypt(value[:200])


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16))  # user / assistant / system – not sensitive
    content_enc: Mapped[bytes] = mapped_column(LargeBinary)
    attachments_meta_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    model_used: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[ChatSession] = relationship(back_populates="messages")

    @property
    def content(self) -> str:
        return crypto.decrypt(self.content_enc)

    @content.setter
    def content(self, value: str) -> None:
        self.content_enc = crypto.encrypt(value)


class SkillStore(Base):
    """Encrypted key/value store for learned skills & preferences."""
    __tablename__ = "skill_store"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value_enc: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    @property
    def value(self) -> str:
        return crypto.decrypt(self.value_enc)

    @value.setter
    def value(self, v: str) -> None:
        self.value_enc = crypto.encrypt(v)


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    detail_enc: Mapped[bytes] = mapped_column(LargeBinary)
    source_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def detail(self) -> str:
        return crypto.decrypt(self.detail_enc)

    @detail.setter
    def detail(self, value: str) -> None:
        self.detail_enc = crypto.encrypt(value)


# ---------- engine / session factory ----------
engine = create_engine(
    f"sqlite:///{settings.db_path}",
    pool_pre_ping=True,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
