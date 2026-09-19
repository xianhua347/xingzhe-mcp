"""Route/file MCP contracts, scope boundaries, and multipart uploads to the official API."""

import asyncio
import base64
import hashlib
import time
from email.parser import BytesParser
from email.policy import default
from typing import Any

import httpx
import pytest
from test_integration import authorize, upstream

from xingzhe_mcp.app import create_app
from xingzhe_mcp.config import Settings
from xingzhe_mcp.files import MAX_FILE_BYTES, fit_bytes, gpx_bytes
from xingzhe_mcp.oauth import SCOPES
from xingzhe_mcp.storage import Store
from xingzhe_mcp.xingzhe import Xingzhe, XingzheError

GPX = (
    '<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1"><trk><trkseg>'
    '<trkpt lat="31" lon="121"/></trkseg></trk></gpx>'
)
# Synthetic header for transport tests; not an actual workout and never sent to production.
FIT = b"\x0e\x20\x00\x00\x00\x00\x00\x00.FIT\x00\x00"
FIT_BASE64 = base64.b64encode(FIT).decode()


def multipart(request: httpx.Request) -> dict[str, bytes]:
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
    )
    fields: dict[str, bytes] = {}
    for part in message.iter_parts():
        value = part.get_payload(decode=True)
        assert isinstance(value, bytes)
        fields[str(part.get_param("name", header="content-disposition"))] = value
    return fields


def test_route_and_upload_tools(settings: Settings) -> None:
    posts: list[str] = []

    def api(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/oauth2/") or path == "/openapi/v1/athlete/info/":
            return upstream(request)
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
        posts.append(path)
        fields = multipart(request)
        if path == "/openapi/v1/routes/gpx/":
            assert fields["gpx_file"] == GPX.encode()
            assert fields["title"] == "测试路书".encode()
            assert fields["uuid"] == b"test-route-uuid"
            assert fields["distance"] == b"1000.0"
            assert b'name="gpx_file"; filename="route.gpx"' in request.content
            return httpx.Response(200, json={"code": 0, "data": {"id": 8}})
        assert path == "/openapi/v1/uploads/"
        assert fields["fit_file"] == FIT
        assert fields["md5"].decode() == hashlib.md5(FIT, usedforsecurity=False).hexdigest()
        assert fields["title"] == b"Ride"
        assert fields["fit_filename"] == b"ride.fit"
        assert "user_id" not in fields and "client_id" not in fields
        return httpx.Response(
            201, json={"code": 0, "data": {"upload_id": 10, "workout_id": 43, "new": True}}
        )

    async def run() -> None:
        app = create_app(settings, transport=httpx.MockTransport(api))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=settings.origin
            ) as client,
        ):
            read_exchange = await authorize(client, settings)
            read_token = (await client.post("/token", data=read_exchange)).json()["access_token"]
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
                writes = tool["name"] in ("create_route_from_gpx", "upload_activity_fit")
                assert tool["annotations"]["readOnlyHint"] is not writes
                if writes:
                    assert tool["annotations"]["idempotentHint"] is False
                    assert tool["_meta"]["securitySchemes"][0]["scopes"] == SCOPES
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
            route_args = {
                "title": "测试路书",
                "gpx": GPX,
                "uuid": "test-route-uuid",
                "distance": 1000,
            }
            fit_args = {"title": "Ride", "fit_base64": FIT_BASE64, "filename": "ride.fit"}
            for name, args in (
                ("create_route_from_gpx", route_args),
                ("upload_activity_fit", fit_args),
            ):
                denied = await call(name, args, read_token)
                assert denied["isError"] and "mcp/www_authenticate" in denied.get("_meta", {}), (
                    denied
                )
            assert posts == []
            assert (await call("create_route_from_gpx", route_args))["structuredContent"]["id"] == 8
            assert (await call("upload_activity_fit", fit_args))["structuredContent"][
                "upload_id"
            ] == 10
            assert (await call("upload_activity_fit", {**fit_args, "filename": "../ride.fit"}))[
                "isError"
            ]
            assert (await call("upload_activity_fit", {**fit_args, "detail": "x" * 801}))["isError"]
            assert (await call("create_route_from_gpx", {**route_args, "sport": 5}))["isError"]
            assert len(posts) == 2
            # A legacy read-only upstream connection cannot be used for writes either.
            await authorize(client, settings)
            denied = await call("upload_activity_fit", fit_args)
            assert denied["isError"] and len(posts) == 2

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


@pytest.mark.parametrize(
    "content",
    [
        "?invalid",
        base64.b64encode(b"not a fit").decode(),
        base64.b64encode(FIT + b"x" * MAX_FILE_BYTES).decode(),
    ],
)
def test_invalid_fit(content: str) -> None:
    with pytest.raises(ValueError):
        fit_bytes(content)


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_upload_timeout_is_not_retried(settings: Settings, failure: str) -> None:
    async def run() -> None:
        calls = 0

        def timeout(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if failure == "server_error":
                return httpx.Response(500, json={"secret": "private diagnostic"})
            raise httpx.ReadTimeout("private diagnostic", request=request)

        store = Store(
            settings.database_url.get_secret_value(), settings.encryption_key.get_secret_value()
        )
        async with store.transaction() as tx:
            await tx.put(
                "connection",
                "xingzhe:123",
                {
                    "access_token": "write-token",
                    "refresh_token": "refresh",
                    "expires_at": time.time() + 3600,
                    "scope": "write",
                },
            )
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            api = Xingzhe(settings, store, client)
            with pytest.raises(XingzheError, match="outcome is unknown"):
                await api.upload_activity("xingzhe:123", "Ride", FIT_BASE64, "ride.fit")
            assert calls == 1

    asyncio.run(run())
