"""Field-level encryption for all persisted sensitive data.

Uses Fernet (AES-128-CBC + HMAC-SHA256 under the hood) with a key derived
from the configured master secret via PBKDF2-HMAC-SHA256. Sensitive columns
(chat content, prompts, logs) are stored only as ciphertext – never plaintext.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

_KDF_SALT = b"ai-platform-db-layer-v1"


class CryptoService:
    """Encrypt/decrypt strings and bytes; safe to use across threads."""

    def __init__(self, master_secret: str | None = None):
        secret = (master_secret or settings.db_encryption_key).encode("utf-8")
        dk = hashlib.pbkdf2_hmac("sha256", secret, _KDF_SALT, 200_000, dklen=32)
        self._fernet = Fernet(base64.urlsafe_b64encode(dk))

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:  # tampered or wrong key
            raise ValueError("Decryption failed: invalid token or key rotation") from exc

    def blind_index(self, value: str) -> str:
        """Deterministic SHA-256 hash for searchable encrypted lookups."""
        return hashlib.sha256((_KDF_SALT + value.encode("utf-8"))).hexdigest()


crypto = CryptoService()
