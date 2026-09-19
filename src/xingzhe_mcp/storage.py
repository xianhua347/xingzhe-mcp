"""Encrypted records in private Supabase Postgres tables."""

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from cryptography.fernet import Fernet
from psycopg import AsyncConnection


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Transaction:
    def __init__(self, connection: AsyncConnection[Any], cipher: Fernet) -> None:
        self.connection = connection
        self.cipher = cipher

    async def get(self, kind: str, key: str) -> dict[str, Any] | None:
        cursor = await self.connection.execute(
            "SELECT payload FROM xingzhe.records WHERE kind = %s AND key_hash = %s "
            "AND (expires_at IS NULL OR expires_at > now()) FOR UPDATE",
            (kind, digest(key)),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        result: dict[str, Any] = json.loads(self.cipher.decrypt(bytes(row[0])))
        return result

    async def put(
        self,
        kind: str,
        key: str,
        value: dict[str, Any],
        *,
        expires_at: float | None = None,
        grant_id: str | None = None,
    ) -> None:
        await self.connection.execute(
            "INSERT INTO xingzhe.records (kind, key_hash, payload, expires_at, grant_id) "
            "VALUES (%s, %s, %s, to_timestamp(%s), %s) "
            "ON CONFLICT (kind, key_hash) DO UPDATE SET payload = EXCLUDED.payload, "
            "expires_at = EXCLUDED.expires_at, grant_id = EXCLUDED.grant_id",
            (
                kind,
                digest(key),
                self.cipher.encrypt(json.dumps(value).encode()),
                expires_at,
                grant_id,
            ),
        )
        await self.connection.execute("DELETE FROM xingzhe.records WHERE expires_at < now()")

    async def delete(self, kind: str, key: str) -> None:
        await self.connection.execute(
            "DELETE FROM xingzhe.records WHERE kind = %s AND key_hash = %s", (kind, digest(key))
        )

    async def revoke(self, grant_id: str) -> None:
        await self.connection.execute(
            "DELETE FROM xingzhe.records WHERE grant_id = %s", (grant_id,)
        )

    async def disconnect(self) -> None:
        await self.connection.execute("DELETE FROM xingzhe.records")


class Store:
    def __init__(self, database_url: str, encryption_key: str) -> None:
        self.database_url = database_url
        self.cipher = Fernet(encryption_key.encode())

    @asynccontextmanager
    async def transaction(self, lock: str = "oauth") -> AsyncIterator[Transaction]:
        # Supabase transaction pooling multiplexes connections: do not prepare statements.
        async with await AsyncConnection.connect(
            self.database_url, prepare_threshold=None, connect_timeout=10
        ) as connection:
            await connection.execute("SET LOCAL lock_timeout = '20s'")
            await connection.execute("SET LOCAL statement_timeout = '25s'")
            # Transaction locks work across processes and Vercel instances, including absent rows.
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"xingzhe:{lock}",)
            )
            yield Transaction(connection, self.cipher)
