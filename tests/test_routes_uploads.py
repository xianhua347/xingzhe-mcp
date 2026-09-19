"""Read-only route, GPX and upload history tools."""

import asyncio
from typing import Any

import httpx
import pytest
from test_integration import authorize, upstream

from xingzhe_mcp.app import create_app
from xingzhe_mcp.config import Settings
from xingzhe_mcp.files import MAX_FILE_BYTES, gpx_bytes
from xingzhe_mcp.oauth import SCOPES
from xingzhe_mcp.storage import Store

GPX = (
    '<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1"><trk><trkseg>'
    '<trkpt lat="31" lon="121"/></trkseg></trk></gpx>'
)


def test_route_and_upload_tools(settings: Settings) -> None:
    def api(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/oauth2/") or path == "/openapi/v1/athlete/info/":
            return upstream(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer upstream-access"
        if path in ("/openapi/v1/routes/mine/", "/openapi/v1/routes/collects/"):
            assert request.url.params["limit"] == "1"
            return httpx.Response(
                200,
                json={
                    "count": 2,
                    "next": "https://evil.test/",
                    "results": [{"id": 7, "title": "Shared route", "sport": 3}],
                },
            )
        if path == "/openapi/v1/routes/7/pro/":
            return httpx.Response(
                200, json={"id": 7, "steps": [{"instruction": "Turn left"}], "elevation_gain": 100}
            )
        if path == "/openapi/v1/routes/7/gpx/":
            return httpx.Response(200, text=GPX, headers={"content-type": "application/gpx+xml"})
        if path == "/openapi/v1/uploads/" and request.method == "GET":
            return httpx.Response(
                200, json={"count": 1, "results": [{"upload_id": 9, "workout_id": 42, "new": True}]}
            )
        raise AssertionError(f"Unexpected upstream path: {path}")

    async def run() -> None:
        app = create_app(settings, transport=httpx.MockTransport(api))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=settings.origin
            ) as client,
        ):
            rejected = await client.post(
                "/register",
                json={
                    "redirect_uris": ["http://localhost:8765/callback"],
                    "scope": "activities:read xingzhe:write",
                },
            )
            assert rejected.status_code == 400
            registration = (
                await client.post(
                    "/register",
                    json={
                        "redirect_uris": ["http://localhost:8765/callback"],
                        "scope": " ".join(SCOPES),
                        "token_endpoint_auth_method": "client_secret_post",
                    },
                )
            ).json()
            exchange = await authorize(
                client, settings, oauth_client=registration, scope=" ".join(SCOPES)
            )
            issued = (await client.post("/token", data=exchange)).json()
            assert set(issued["scope"].split()) == set(SCOPES)

            tools = (
                await client.post(
                    "/mcp",
                    headers={
                        "Authorization": f"Bearer {issued['access_token']}",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                )
            ).json()["result"]["tools"]
            for tool in tools:
                assert tool["annotations"]["readOnlyHint"] is True
                assert tool["_meta"]["securitySchemes"][0]["scopes"] == SCOPES
            assert {"create_route_from_gpx", "upload_activity_fit"}.isdisjoint(
                tool["name"] for tool in tools
            )
            # A refresh grant issued before writes were removed must now yield read-only tokens.
            store = Store(
                settings.database_url.get_secret_value(), settings.encryption_key.get_secret_value()
            )
            async with store.transaction() as tx:
                grant = await tx.get("refresh", issued["refresh_token"])
                assert grant is not None
                grant["scopes"].append("xingzhe:write")
                await tx.put("refresh", issued["refresh_token"], grant)
            renewed = await client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": issued["refresh_token"],
                    "client_id": registration["client_id"],
                    "client_secret": registration["client_secret"],
                },
            )
            assert renewed.status_code == 200
            issued = renewed.json()
            assert set(issued["scope"].split()) == set(SCOPES)

            async def call(
                name: str, args: dict[str, Any], token: str = issued["access_token"]
            ) -> dict[str, Any]:
                result = await client.post(
                    "/mcp",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": args},
                    },
                )
                assert result.status_code == 200
                value: dict[str, Any] = result.json()["result"]
                return value

            for name in ("list_my_routes", "list_collected_routes"):
                result = (await call(name, {"limit": 1}))["structuredContent"]
                assert result["results"][0]["id"] == 7
                assert result["next_offset"] == 1
                assert (await call(name, {"limit": 21}))["isError"]
            assert (await call("get_route", {"route_id": 7}))["structuredContent"]["steps"][0][
                "instruction"
            ] == "Turn left"
            assert (await call("get_route_gpx", {"route_id": 7}))["structuredContent"]["gpx"] == GPX
            assert (await call("list_uploads", {}))["structuredContent"]["results"][0][
                "upload_id"
            ] == 9
            for name in ("create_route_from_gpx", "upload_activity_fit"):
                assert (await call(name, {}))["isError"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "content",
    [
        "",
        "<html/>",
        '<!DOCTYPE gpx [<!ENTITY x "y">]><gpx>&x;</gpx>',
        "<gpx>" + "x" * MAX_FILE_BYTES + "</gpx>",
    ],
)
def test_invalid_gpx(content: str) -> None:
    with pytest.raises(ValueError):
        gpx_bytes(content)
