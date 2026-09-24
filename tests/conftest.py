"""Pytest fixtures: isolated temp environment + FastAPI TestClient.

The whole backend is imported *after* AIP_* env vars point at a throwaway
data directory, so tests never touch the production database or keyfiles.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# --- isolate environment BEFORE importing any ai_platform module -----------
_TMP = tempfile.mkdtemp(prefix="aip-tests-")
os.environ["AIP_DB_PATH"] = str(Path(_TMP) / "test.db")
os.environ["AIP_JWT_SECRET"] = "test-jwt-secret-not-for-production"
os.environ["AIP_DB_ENCRYPTION_KEY"] = ""           # auto-generate into tmp dir
os.environ["AIP_RATE_LIMIT_PER_MINUTE"] = "10000"  # tests hammer the API
os.environ["AIP_ENABLE_CLOUD_MODELS"] = "false"    # deterministic local-only


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from ai_platform.api.main import app

    with TestClient(app) as c:      # runs lifespan (init_db, plugins, skills)
        yield c


@pytest.fixture(scope="session")
def token(client) -> str:
    r = client.post("/api/auth/register",
                    json={"email": "tester@example.com", "password": "SuperSecret123!"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="session")
def auth(token) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def admin_token(client) -> str:
    """Create an admin user directly in the DB and log in via the API."""
    from ai_platform.db.models import SessionLocal
    from ai_platform.db.repository import Repo

    db = SessionLocal()
    try:
        repo = Repo(db)
        if not repo.get_user_by_email("admin@test.local"):
            repo.create_user("admin@test.local", "AdminPass123!", is_admin=True)
    finally:
        db.close()
    r = client.post("/api/auth/login",
                    json={"email": "admin@test.local", "password": "AdminPass123!"})
    assert r.status_code == 200, r.text
    return r.json()["token"]
