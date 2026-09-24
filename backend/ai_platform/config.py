"""Central configuration for the AI platform backend.

All secrets are read from environment variables or persisted to a local
keyfile with 0600 permissions. Nothing sensitive is hard-coded.
"""
from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_or_create_secret(env_name: str, filename: str) -> str:
    """Return secret from env, or generate & persist one (0600)."""
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    path = DATA_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)
    return value


class Settings(BaseSettings):
    app_name: str = "AI Platform"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # Database
    db_path: Path = DATA_DIR / "platform.db"
    db_encryption_key: str = ""          # Fernet key (base64), auto-generated
    jwt_secret: str = ""                 # token signing secret, auto-generated
    session_ttl_seconds: int = 60 * 60 * 12

    # Security layer
    rate_limit_per_minute: int = 60
    max_payload_bytes: int = 2_000_000
    block_on_critical: bool = True

    # Model providers (optional – local fallback always available)
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    qwen_api_key: str = ""
    enable_cloud_models: bool = True

    # Resources
    max_context_messages: int = 40
    cache_max_entries: int = 512

    class Config:
        env_prefix = "AIP_"
        env_file = str(BASE_DIR / ".env")
        extra = "ignore"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    if not s.db_encryption_key:
        s.db_encryption_key = _load_or_create_secret("AIP_DB_ENCRYPTION_KEY", "db.key")
    if not s.jwt_secret:
        s.jwt_secret = _load_or_create_secret("AIP_JWT_SECRET", "jwt.key")
    return s


settings = get_settings()
