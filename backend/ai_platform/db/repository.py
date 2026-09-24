"""Encrypted persistence helpers (repository pattern)."""
from __future__ import annotations

import json
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ChatSession, Message, SecurityEvent, SkillStore, User, utcnow


class Repo:
    def __init__(self, db: Session):
        self.db = db

    # users -----------------------------------------------------------------
    def get_user_by_email(self, email: str) -> User | None:
        from .crypto import crypto
        return self.db.scalar(select(User).where(User.email_index == crypto.blind_index(email.lower().strip())))

    def create_user(self, email: str, password: str, is_admin: bool = False) -> User:
        u = User(is_admin=is_admin)
        u.email = email
        u.set_password(password)
        self.db.add(u)
        self.db.commit()
        self.db.refresh(u)
        return u

    # sessions ---------------------------------------------------------------
    def list_sessions(self, user_id: int) -> Iterable[ChatSession]:
        stmt = select(ChatSession).where(ChatSession.user_id == user_id).order_by(
            ChatSession.pinned.desc(), ChatSession.updated_at.desc()
        )
        return self.db.scalars(stmt).all()

    def get_session(self, public_id: str, user_id: int) -> ChatSession | None:
        return self.db.scalar(
            select(ChatSession).where(ChatSession.public_id == public_id, ChatSession.user_id == user_id)
        )

    def create_session(self, user_id: int, title: str = "New chat", model: str = "auto") -> ChatSession:
        s = ChatSession(user_id=user_id, model=model)
        s.title = title
        self.db.add(s)
        self.db.commit()
        self.db.refresh(s)
        return s

    def delete_session(self, session: ChatSession) -> None:
        self.db.delete(session)
        self.db.commit()

    # messages ---------------------------------------------------------------
    def add_message(self, session: ChatSession, role: str, content: str,
                    model_used: str = "", tokens_in: int = 0, tokens_out: int = 0,
                    latency_ms: float = 0.0, attachments: dict | None = None) -> Message:
        seq = len(session.messages)
        m = Message(session_id=session.id, role=role, seq=seq, model_used=model_used,
                    tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms)
        m.content = content
        if attachments:
            m.attachments_meta_enc = _enc_json(attachments)
        self.db.add(m)
        session.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(m)
        return m

    def recent_messages(self, session: ChatSession, limit: int) -> list[Message]:
        return list(session.messages)[-limit:]

    # security log -------------------------------------------------------------
    def log_security_event(self, severity: str, category: str, detail: str, source_ip: str = "") -> None:
        ev = SecurityEvent(severity=severity, category=category, source_ip=source_ip)
        ev.detail = detail[:4000]
        self.db.add(ev)
        self.db.commit()

    def recent_security_events(self, limit: int = 100) -> list[dict]:
        rows = self.db.scalars(select(SecurityEvent).order_by(SecurityEvent.id.desc()).limit(limit)).all()
        return [{"id": r.id, "severity": r.severity, "category": r.category,
                 "detail": r.detail, "source_ip": r.source_ip, "created_at": r.created_at.isoformat()}
                for r in rows]

    # encrypted key/value store (learned skills, preferences) ------------------
    def get_setting(self, key: str) -> str | None:
        row = self.db.scalar(select(SkillStore).where(SkillStore.key == key))
        return row.value if row else None

    def set_setting(self, key: str, value: str) -> None:
        row = self.db.scalar(select(SkillStore).where(SkillStore.key == key))
        if row is None:
            row = SkillStore(key=key)
            self.db.add(row)
        row.value = value
        self.db.commit()


def _enc_json(obj: dict) -> bytes:
    from .crypto import crypto
    return crypto.encrypt(json.dumps(obj, ensure_ascii=False))


def dec_json(blob: bytes | None) -> dict | None:
    if not blob:
        return None
    from .crypto import crypto
    return json.loads(crypto.decrypt(blob))
