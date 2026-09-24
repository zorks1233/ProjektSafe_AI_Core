"""JWT auth (HS256) implemented on top of `cryptography` – no external JWT lib.

Tokens: base64url(header).base64url(payload).signature
Payload contains: sub (user id), email, admin flag, exp, iat, jti.
"""
from __future__ import annotations

import base64
import json
import secrets
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.hmac import HMAC

from ..config import settings


class AuthError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(msg: bytes) -> bytes:
    h = HMAC(settings.jwt_secret.encode(), hashes.SHA256())
    h.update(msg)
    return h.finalize()


def create_token(user_id: int, email: str, is_admin: bool = False,
                 ttl: int | None = None) -> str:
    ttl = ttl or settings.session_ttl_seconds
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id), "email": email, "admin": is_admin,
        "iat": now, "exp": now + ttl, "jti": secrets.token_hex(8),
    }
    seg = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    sig = _sign(seg.encode())
    return f"{seg}.{_b64(sig)}"


def verify_token(token: str) -> dict:
    try:
        h, p, s = token.split(".")
    except ValueError as exc:
        raise AuthError("malformed token") from exc
    expected = _sign(f"{h}.{p}".encode())
    got = _unb64(s)
    if not secrets.compare_digest(expected, got):
        raise AuthError("bad signature")
    payload = json.loads(_unb64(p))
    if payload.get("exp", 0) < time.time():
        raise AuthError("token expired")
    return payload
