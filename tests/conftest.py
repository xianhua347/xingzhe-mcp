"""Integration tests require an isolated database whose name ends in _test."""

import os
from pathlib import Path

import psycopg
import pytest
from cryptography.fernet import Fernet

from xingzhe_mcp.config import Settings


@pytest.fixture
def settings() -> Settings:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with psycopg.connect(database_url, autocommit=True) as connection:
        assert connection.info.dbname.endswith("_test"), "Use an isolated *_test database"
        connection.execute("DROP SCHEMA IF EXISTS xingzhe CASCADE")
        for migration in sorted(Path("supabase/migrations").glob("*.sql")):
            connection.execute(migration.read_text())
    return Settings.model_validate(
        {
            "public_url": "http://localhost:8000",
            "DATABASE_URL": database_url,
            "encryption_key": Fernet.generate_key().decode(),
            "xingzhe_client_id": "test-xingzhe",
            "xingzhe_client_secret": "test-upstream-secret",
            "mcp_client_secret": "b" * 40,
            "mcp_redirect_uris": ["http://localhost:8765/callback"],
        }
    )
