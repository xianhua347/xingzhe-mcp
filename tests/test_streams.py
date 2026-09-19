"""Read-only stream transport, account isolation and aligned MCP pagination."""

import asyncio
from typing import Any

import httpx
import pytest
from test_integration import authorize, upstream

from xingzhe_mcp.app import create_app
from xingzhe_mcp.config import Settings


def test_stream_mcp_pagination_and_ownership(settings: Settings) -> None:
    calls: list[str] = []
    samples = {
        "timestamp": [100, 100, 102, 110],
        "cadence": [0, 70, 98, 60],
        "location": [[1, 2], [2, 3], [3, 4], [4, 5]],
        "power": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/stream/"):
            calls.append("stream")
            assert calls[-2] == "owner"
            assert request.method == "POST"
            assert request.content == b"{}"
            assert request.headers["authorization"] == "Bearer upstream-access"
            return httpx.Response(200, json={"code": 0, "data": samples})
        if path in ("/openapi/v1/activities/42/", "/openapi/v1/activities/99/"):
            calls.append("owner")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": 42 if "42" in path else 99,
                        "user_id": 123 if "42" in path else 456,
                        "avg_cadence": 0,
                        "max_cadence": 0,
                    }
                },
            )
        return upstream(request)

    async def run() -> None:
        app = create_app(settings, transport=httpx.MockTransport(handler))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=settings.origin
            ) as client,
        ):
            tokens = (await client.post("/token", data=await authorize(client, settings))).json()
            headers = {
                "Authorization": f"Bearer {tokens['access_token']}",
                "Accept": "application/json, text/event-stream",
            }

            async def call(name: str = "get_activity_stream", **arguments: Any) -> dict[str, Any]:
                response = await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": {"activity_id": 42, **arguments}},
                    },
                )
                assert response.status_code == 200
                result: dict[str, Any] = response.json()["result"]
                return result

            summary = await call("get_activity")
            assert summary["structuredContent"]["avg_cadence"] == 0
            first = (await call(fields=["cadence", "power"], limit=2))["structuredContent"]
            assert first["total_points"] == 4
            assert first["next_offset"] == 2
            assert first["streams"] == {"timestamp": [100, 100], "cadence": [0, 70], "power": []}
            assert first["unavailable_streams"] == ["power"]
            last = (await call(fields=["cadence"], limit=2, offset=2))["structuredContent"]
            assert last["streams"] == {"timestamp": [102, 110], "cadence": [98, 60]}
            assert last["next_offset"] is None
            beyond = (await call(offset=10))["structuredContent"]
            assert all(value == [] for value in beyond["streams"].values())
            assert beyond["next_offset"] is None
            before = calls.count("stream")
            assert (await call(activity_id=99))["isError"]
            assert calls.count("stream") == before
            for arguments in ({"fields": ["unknown"]}, {"limit": 1001}, {"offset": -1}):
                assert (await call(**arguments))["isError"]
            assert calls.count("stream") == before

    asyncio.run(run())


@pytest.mark.parametrize(
    "samples",
    [
        {},
        {"timestamp": "invalid"},
        {"timestamp": [1, 2], "cadence": [70]},
    ],
)
def test_malformed_streams_fail(settings: Settings, samples: dict[str, Any]) -> None:
    from unittest.mock import AsyncMock

    from xingzhe_mcp.storage import Store
    from xingzhe_mcp.xingzhe import Xingzhe, XingzheError

    async def run() -> None:
        store = Store(
            settings.database_url.get_secret_value(), settings.encryption_key.get_secret_value()
        )
        async with httpx.AsyncClient() as client:
            xingzhe = Xingzhe(settings, store, client)
            xingzhe.get_activity = AsyncMock()  # type: ignore[method-assign]
            xingzhe.response = AsyncMock(return_value=httpx.Response(200, json={"data": samples}))  # type: ignore[method-assign]
            with pytest.raises(XingzheError, match="activity stream"):
                await xingzhe.get_activity_stream("xingzhe:123", 42)

    asyncio.run(run())
