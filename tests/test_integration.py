"""Exercise HTTP OAuth and MCP against PostgreSQL, mocking only the external Xingzhe API."""

import asyncio
import base64
import hashlib
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from mcp.server.auth.provider import AuthorizationParams, TokenError
from pydantic import AnyUrl

from xingzhe_mcp.app import create_app
from xingzhe_mcp.config import Settings
from xingzhe_mcp.oauth import SCOPE, OAuthProvider
from xingzhe_mcp.storage import Store
from xingzhe_mcp.xingzhe import Tokens, Xingzhe

VERIFIER = "x" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)


def upstream(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/oauth2/v2/access_token/":
        assert request.headers["authorization"] == "Bearer test-xingzhe:test-upstream-secret"
        assert "multipart/form-data" in request.headers["content-type"]
        return httpx.Response(
            200,
            json={
                "access_token": "upstream-access",
                "refresh_token": "upstream-refresh",
                "expires_in": 3600,
            },
        )
    assert request.headers["authorization"] == "Bearer upstream-access"
    if request.url.path == "/openapi/v1/activities/":
        assert request.url.params.get("limit") in ("1", "20")
        return httpx.Response(
            200,
            json={
                "count": 2,
                "next": "https://untrusted.example/",
                "results": [{"id": 42, "title": "Test ride", "distance": 12000}],
            },
        )
    if request.url.path == "/openapi/v1/activities/42/":
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"id": 42, "duration": 1800, "avg_cadence": 82, "max_cadence": 106},
            },
        )
    raise AssertionError(f"Unexpected upstream path: {request.url.path}")


async def form(
    client: httpx.AsyncClient, path: str, settings: Settings, **values: str
) -> httpx.Response:
    page = await client.get(path)
    assert page.status_code == 200, page.text
    return await client.post(
        urlsplit(path).path,
        data={
            "csrf": client.cookies["xingzhe_csrf"],
            "admin_key": settings.admin_key.get_secret_value(),
            **values,
        },
    )


async def connect(client: httpx.AsyncClient, settings: Settings) -> None:
    redirect = await form(client, "/connect", settings)
    assert redirect.status_code == 303
    state = parse_qs(urlsplit(redirect.headers["location"]).query)["state"][0]
    invalid = await client.get("/oauth/xingzhe/callback", params={"state": "wrong", "code": "code"})
    assert invalid.status_code == 400
    result = await client.get("/oauth/xingzhe/callback", params={"state": state, "code": "code"})
    assert result.status_code == 200, result.text
    replay = await client.get("/oauth/xingzhe/callback", params={"state": state, "code": "code"})
    assert replay.status_code == 400


async def authorize(client: httpx.AsyncClient, settings: Settings) -> dict[str, str]:
    redirect_uri = str(settings.mcp_redirect_uris[0])
    response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": settings.mcp_client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": "client-state",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": settings.resource,
        },
    )
    assert response.status_code == 302, response.text
    consent_url = response.headers["location"]
    ticket = parse_qs(urlsplit(consent_url).query)["ticket"][0]
    approval = await form(client, consent_url, settings, ticket=ticket)
    assert approval.status_code == 303, approval.text
    query = parse_qs(urlsplit(approval.headers["location"]).query)
    assert query["state"] == ["client-state"]
    return {
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "client_id": settings.mcp_client_id,
        "client_secret": settings.mcp_client_secret.get_secret_value(),
        "redirect_uri": redirect_uri,
        "code_verifier": VERIFIER,
        "resource": settings.resource,
    }


def make_app(settings: Settings) -> FastAPI:
    return create_app(settings, transport=httpx.MockTransport(upstream))


def test_oauth_mcp_end_to_end(settings: Settings) -> None:
    async def run() -> None:
        app = make_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=settings.origin,
            ) as client,
        ):
            assert (await client.get("/ready")).status_code == 200
            metadata = (await client.get("/.well-known/oauth-authorization-server")).json()
            assert metadata["code_challenge_methods_supported"] == ["S256"]
            assert "registration_endpoint" not in metadata
            resource = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
            assert resource["resource"] == settings.resource
            assert (await client.post("/mcp", json={})).status_code == 401
            assert (await client.get("/api/activities")).status_code == 401
            await connect(client, settings)
            exchange = await authorize(client, settings)
            assert (
                await client.post("/token", data={**exchange, "code_verifier": "bad"})
            ).status_code == 400
            assert (
                await client.post("/token", data={**exchange, "resource": "https://evil.test"})
            ).status_code == 400
            token_response = await client.post("/token", data=exchange)
            assert token_response.status_code == 200, token_response.text
            tokens = token_response.json()
            assert tokens["access_token"] != "upstream-access"
            assert (await client.post("/token", data=exchange)).status_code == 400
            headers = {
                "Authorization": f"Bearer {tokens['access_token']}",
                "Accept": "application/json, text/event-stream",
            }
            initialized = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "integration-test", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200, initialized.text
            tools = await client.post(
                "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
            )
            assert {tool["name"] for tool in tools.json()["result"]["tools"]} == {
                "list_activities",
                "get_activity",
            }
            result = await client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "get_activity", "arguments": {"activity_id": 42}},
                },
            )
            assert result.status_code == 200, result.text
            assert result.json()["result"]["structuredContent"]["avg_cadence"] == 82
            page = await client.get("/api/activities?limit=1", headers=headers)
            assert page.json()["next_offset"] == 1
            assert (
                await client.get(
                    "/api/activities?start_timestamp=2&end_timestamp=1", headers=headers
                )
            ).status_code == 422
            # A new process reads the same durable grant.
            restarted = make_app(settings)
            async with (
                restarted.router.lifespan_context(restarted),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=restarted),
                    base_url=settings.origin,
                ) as other,
            ):
                assert (await other.get("/api/activities/42", headers=headers)).status_code == 200
            refresh_data = {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": settings.mcp_client_id,
                "client_secret": settings.mcp_client_secret.get_secret_value(),
            }
            renewed = await client.post("/token", data=refresh_data)
            assert renewed.status_code == 200, renewed.text
            assert (await client.post("/token", data=refresh_data)).status_code == 400
            assert (await client.get("/api/activities", headers=headers)).status_code == 401
            await form(client, "/disconnect", settings)
            new_headers = {"Authorization": f"Bearer {renewed.json()['access_token']}"}
            assert (await client.get("/api/activities", headers=new_headers)).status_code == 401

    asyncio.run(run())


def test_browser_and_client_boundaries(settings: Settings) -> None:
    async def run() -> None:
        app = make_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=settings.origin,
            ) as client,
        ):
            assert (
                await client.post(
                    "/connect", data={"admin_key": settings.admin_key.get_secret_value()}
                )
            ).status_code == 403
            await client.get("/connect")
            assert (
                await client.post(
                    "/connect", data={"csrf": client.cookies["xingzhe_csrf"], "admin_key": "wrong"}
                )
            ).status_code == 403
            bad = await client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": settings.mcp_client_id,
                    "redirect_uri": "https://evil.test/callback",
                    "code_challenge": CHALLENGE,
                    "code_challenge_method": "S256",
                },
            )
            assert bad.status_code == 400
            assert "location" not in bad.headers
            redirect = await form(client, "/connect", settings)
            state = parse_qs(urlsplit(redirect.headers["location"]).query)["state"][0]
            client.cookies.delete("xingzhe_oauth")
            assert (
                await client.get("/oauth/xingzhe/callback", params={"state": state, "code": "code"})
            ).status_code == 400

    asyncio.run(run())


def test_atomic_code_exchange_and_encryption(settings: Settings) -> None:
    async def run() -> None:
        store = Store(
            settings.database_url.get_secret_value(), settings.encryption_key.get_secret_value()
        )
        provider = OAuthProvider(settings, store)
        client = await provider.get_client(settings.mcp_client_id)
        assert client is not None
        consent = await provider.authorize(
            client,
            AuthorizationParams(
                state="state",
                scopes=[SCOPE],
                code_challenge=CHALLENGE,
                redirect_uri=AnyUrl(str(settings.mcp_redirect_uris[0])),
                redirect_uri_provided_explicitly=True,
            ),
        )
        ticket = parse_qs(urlsplit(consent).query)["ticket"][0]
        redirect = await provider.approve(ticket)
        code = parse_qs(urlsplit(redirect).query)["code"][0]
        loaded = await provider.load_authorization_code(client, code)
        assert loaded is not None
        results = await asyncio.gather(
            provider.exchange_authorization_code(client, loaded),
            provider.exchange_authorization_code(client, loaded),
            return_exceptions=True,
        )
        assert sum(isinstance(result, TokenError) for result in results) == 1
        with psycopg.connect(settings.database_url.get_secret_value()) as db:
            rows = db.execute("SELECT key_hash, payload FROM xingzhe.records").fetchall()
            assert rows
            assert all(code not in row[0] and b'"token"' not in bytes(row[1]) for row in rows)
            rls = db.execute(
                "SELECT relrowsecurity FROM pg_class WHERE oid='xingzhe.records'::regclass"
            ).fetchone()
            assert rls and rls[0]

    asyncio.run(run())


def test_concurrent_upstream_refresh(settings: Settings) -> None:
    async def run() -> None:
        calls = 0

        async def refresh(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.03)
            return upstream(request)

        store = Store(
            settings.database_url.get_secret_value(), settings.encryption_key.get_secret_value()
        )
        async with store.transaction("connection") as tx:
            await tx.put(
                "connection",
                "owner",
                Tokens(
                    access_token="expired", refresh_token="old-refresh", expires_at=time.time() - 1
                ).model_dump(),
            )
        async with httpx.AsyncClient(transport=httpx.MockTransport(refresh)) as client:
            xingzhe = Xingzhe(settings, store, client)
            results = await asyncio.gather(xingzhe.access_token(), xingzhe.access_token())
            assert all(token == "upstream-access" for token in results)
            assert calls == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"code": 401}, 401),
        (200, {"code": 400, "msg": "API Limited"}, 429),
        (500, {"secret": "must not leak"}, 502),
        (200, [1, 2], 502),
    ],
)
def test_upstream_errors_are_sanitized(status: int, body: Any, expected: int) -> None:
    from xingzhe_mcp.xingzhe import XingzheError

    with pytest.raises(XingzheError) as raised:
        Xingzhe._response(httpx.Response(status, json=body))
    assert raised.value.status == expected
    assert "must not leak" not in str(raised.value)
