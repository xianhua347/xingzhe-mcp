"""Platform connection strings must work with libpq without losing SSL settings."""

from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr

from xingzhe_mcp.config import Settings


def test_supabase_vercel_pooler_url() -> None:
    raw = SecretStr(
        "postgresql://postgres.project:p%40ss@pooler.example:6543/postgres"
        "?supa=base-pooler.x&sslmode=require"
    )
    normalized = Settings.normalize_database_url(raw)
    params = conninfo_to_dict(normalized.get_secret_value())
    assert params["sslmode"] == "require"
    assert params["port"] == "6543"
    assert params["password"] == "p@ss"
    assert "supa" not in params
