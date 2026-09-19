"""Public HTTP contract checks."""

import asyncio

from httpx import ASGITransport, AsyncClient, Response

from app import app


def test_health() -> None:
    async def request_health() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.get("/health")

    response = asyncio.run(request_health())

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
