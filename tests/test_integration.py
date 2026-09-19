"""Exercise HTTP OAuth and MCP against PostgreSQL, mocking only the external Xingzhe API."""

import asyncio
import base64
import hashlib
import re
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
from xingzhe_mcp.oauth import SCOPE, SCOPES, OAuthProvider
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
    if request.url.path == "/openapi/v1/athlete/info/":
        return httpx.Response(200, json={"id": 123, "username": "测试骑友"})
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
                "data": {
                    "id": 42,
                    "user_id": 123,
                    "duration": 1800,
                    "avg_cadence": 82,
                    "max_cadence": 106,
                },
            },
        )
    raise AssertionError(f"Unexpected upstream path: {request.url.path}")


async def consent(client: httpx.AsyncClient, url: str) -> httpx.Response:
    page = await client.get(url)
    if page.status_code == 303:
        assert urlsplit(page.headers["location"]).hostname == "www.imxingzhe.com"
        return page
    assert page.status_code == 200
    assert page.headers["referrer-policy"] == "same-origin"
    # Chromium applies form-action to the POST redirect destination as well.
    assert "form-action 'self' https://www.imxingzhe.com" in page.headers["content-security-policy"]
    ticket = parse_qs(urlsplit(url).query)["ticket"][0]
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert csrf
    return await client.post("/connect", data={"ticket": ticket, "csrf": csrf[1]})


async def authorize(
    client: httpx.AsyncClient,
    settings: Settings,
    code: str = "code",
    oauth_client: dict[str, Any] | None = None,
    scope: str = SCOPE,
) -> dict[str, str]:
    credentials = oauth_client or {"client_id": "test-client", "client_secret": "b" * 40}
    redirect_uri = "http://localhost:8765/callback"
    response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": credentials["client_id"],
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": "client-state",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "resource": settings.resource,
        },
    )
    assert response.status_code == 302, response.text
    redirect = await consent(client, response.headers["location"])
    assert redirect.status_code == 303
    state = parse_qs(urlsplit(redirect.headers["location"]).query)["state"][0]
    assert urlsplit(redirect.headers["location"]).hostname == "www.imxingzhe.com"
    expected_scope = "write" if "xingzhe:write" in scope.split() else "read"
    assert parse_qs(urlsplit(redirect.headers["location"]).query)["scope"] == [expected_scope]
    approval = await client.get("/oauth/xingzhe/callback", params={"state": state, "code": code})
    assert approval.status_code == 303, approval.text
    query = parse_qs(urlsplit(approval.headers["location"]).query)
    assert query["state"] == ["client-state"]
    replay = await client.get("/oauth/xingzhe/callback", params={"state": state, "code": code})
    assert replay.status_code == 400
    return {
        "grant_type": "authorization_code",
        "code": query["code"][0],
        "client_id": credentials["client_id"],
        "client_secret": credentials["client_secret"],
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
            assert metadata["registration_endpoint"] == settings.origin + "/register"
            resource = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
            assert resource["resource"] == settings.resource
            assert resource["scopes_supported"] == SCOPES
            assert (await client.post("/mcp", json={})).status_code == 401
            assert (await client.get("/api/activities")).status_code == 401
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
                "get_profile",
                "list_my_routes",
                "list_collected_routes",
                "get_route",
                "get_route_gpx",
                "create_route_from_gpx",
                "list_uploads",
                "upload_activity_fit",
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
                "client_id": "test-client",
                "client_secret": "b" * 40,
            }
            renewed = await client.post("/token", data=refresh_data)
            assert renewed.status_code == 200, renewed.text
            assert (await client.post("/token", data=refresh_data)).status_code == 400
            assert (await client.get("/api/activities", headers=headers)).status_code == 401
            new_headers = {"Authorization": f"Bearer {renewed.json()['access_token']}"}
            assert (await client.post("/disconnect", headers=new_headers)).status_code == 200
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
            assert (await client.post("/disconnect")).status_code == 401
            assert (await client.get("/connect", params={"ticket": "wrong"})).status_code == 400
            bad = await client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": "test-client",
                    "redirect_uri": "https://evil.test/callback",
                    "code_challenge": CHALLENGE,
                    "code_challenge_method": "S256",
                },
            )
            assert bad.status_code == 400
            assert "location" not in bad.headers
            response = await client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": "test-client",
                    "redirect_uri": "http://localhost:8765/callback",
                    "code_challenge": CHALLENGE,
                    "code_challenge_method": "S256",
                },
            )
            redirect = await consent(client, response.headers["location"])
            state = parse_qs(urlsplit(redirect.headers["location"]).query)["state"][0]
            saved_cookies = httpx.Cookies(client.cookies)
            client.cookies.clear()
            assert (
                await client.get("/oauth/xingzhe/callback", params={"state": state, "code": "code"})
            ).status_code == 400
            client.cookies.update(saved_cookies)
            assert (
                await client.get(
                    "/oauth/xingzhe/callback", params={"state": state, "error": "access_denied"}
                )
            ).status_code == 400
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
        client = await provider.get_client("test-client")
        assert client is not None
        consent = await provider.authorize(
            client,
            AuthorizationParams(
                state="state",
                scopes=[SCOPE],
                code_challenge=CHALLENGE,
                redirect_uri=AnyUrl("http://localhost:8765/callback"),
                redirect_uri_provided_explicitly=True,
            ),
        )
        ticket = parse_qs(urlsplit(consent).query)["ticket"][0]
        redirect = await provider.approve(
            ticket,
            "xingzhe:123",
            Tokens(
                access_token="upstream-access",
                refresh_token="upstream-refresh",
                expires_at=time.time() + 3600,
            ),
        )
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
                "xingzhe:123",
                Tokens(
                    access_token="expired", refresh_token="old-refresh", expires_at=time.time() - 1
                ).model_dump(),
            )
        async with httpx.AsyncClient(transport=httpx.MockTransport(refresh)) as client:
            xingzhe = Xingzhe(settings, store, client)
            results = await asyncio.gather(
                xingzhe.access_token("xingzhe:123"), xingzhe.access_token("xingzhe:123")
            )
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


def test_accounts_are_isolated(settings: Settings) -> None:
    def accounts(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/v2/access_token/":
            account = 2 if b"account-two" in request.content else 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"account-{account}",
                    "refresh_token": f"refresh-{account}",
                    "expires_in": 3600,
                },
            )
        account = int(request.headers["authorization"].removeprefix("Bearer account-"))
        if request.url.path == "/openapi/v1/athlete/info/":
            return httpx.Response(200, json={"code": 200, "data": {"id": account}, "msg": "OK"})
        if request.url.path == "/openapi/v1/activities/":
            return httpx.Response(200, json={"count": 1, "results": [{"id": account * 100}]})
        if request.url.path == "/openapi/v1/activities/200/":
            # Upstream might expose a public ride belonging to another account.
            return httpx.Response(200, json={"data": {"id": 200, "user_id": 2}})
        raise AssertionError(request.url.path)

    async def run() -> None:
        app = create_app(settings, transport=httpx.MockTransport(accounts))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=settings.origin,
            ) as first,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url=settings.origin,
            ) as second,
        ):
            tokens = []
            for client, code in ((first, "account-one"), (second, "account-two")):
                exchange = await authorize(client, settings, code)
                result = await client.post("/token", data=exchange)
                assert result.status_code == 200
                tokens.append(result.json())
            headers = [
                {
                    "Authorization": f"Bearer {t['access_token']}",
                    "Accept": "application/json, text/event-stream",
                }
                for t in tokens
            ]

            async def ride_ids() -> list[int]:
                responses = await asyncio.gather(
                    *[
                        first.post(
                            "/mcp",
                            headers=h,
                            json={
                                "jsonrpc": "2.0",
                                "id": index,
                                "method": "tools/call",
                                "params": {"name": "list_activities", "arguments": {}},
                            },
                        )
                        for index, h in enumerate(headers)
                    ]
                )
                return [
                    r.json()["result"]["structuredContent"]["results"][0]["id"] for r in responses
                ]

            assert await ride_ids() == [100, 200]
            assert (await first.get("/api/activities/200", headers=headers[0])).status_code == 403
            assert (await second.get("/api/activities/200", headers=headers[1])).status_code == 200
            # Reauthorizing A preserves B and cannot rebind an existing A token to B.
            await authorize(first, settings, "account-one")
            assert await ride_ids() == [100, 200]
            refresh = {
                "grant_type": "refresh_token",
                "refresh_token": tokens[0]["refresh_token"],
                "client_id": "test-client",
                "client_secret": "b" * 40,
            }
            renewed = await first.post("/token", data=refresh)
            assert renewed.status_code == 200
            headers[0]["Authorization"] = f"Bearer {renewed.json()['access_token']}"
            assert await ride_ids() == [100, 200]
            assert (await first.post("/disconnect", headers=headers[0])).status_code == 200
            assert (await first.get("/api/activities", headers=headers[0])).status_code == 401
            assert (await second.get("/api/activities", headers=headers[1])).json()["results"][0][
                "id"
            ] == 200
            refresh["refresh_token"] = renewed.json()["refresh_token"]
            assert (await first.post("/token", data=refresh)).status_code == 400
            exchange = await authorize(first, settings, "account-one")
            assert (await first.post("/token", data=exchange)).status_code == 200
            assert (await first.get("/api/activities", headers=headers[0])).status_code == 401
            revoked = await second.post(
                "/revoke",
                data={
                    "client_id": "test-client",
                    "client_secret": "b" * 40,
                    "token": tokens[1]["access_token"],
                },
            )
            assert revoked.status_code == 200
            assert (await second.get("/api/activities", headers=headers[1])).status_code == 401

    asyncio.run(run())


def test_dynamic_registration_and_profile(settings: Settings) -> None:
    async def run() -> None:
        display_name = "骑友甲"

        def profile_upstream(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/openapi/v1/athlete/info/":
                return httpx.Response(200, json={"data": {"id": 123, "username": display_name}})
            return upstream(request)

        app = create_app(settings, transport=httpx.MockTransport(profile_upstream))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=settings.origin
            ) as client,
        ):
            registration = {
                "client_name": "Test <client>",
                "redirect_uris": ["http://localhost:8765/callback"],
                "token_endpoint_auth_method": "client_secret_post",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            }
            for uri in (
                "http://evil.test/callback",
                "https://user:password@evil.test/cb",
                "https://evil.test/#fragment",
            ):
                rejected = await client.post(
                    "/register", json={**registration, "redirect_uris": [uri]}
                )
                assert rejected.status_code == 400
            registered = await client.post("/register", json=registration)
            assert registered.status_code == 201, registered.text
            first = registered.json()
            second = (await client.post("/register", json=registration)).json()
            assert first["client_id"] != second["client_id"]
            assert first.get("client_secret_expires_at") in (None, 0)
            exchange = await authorize(client, settings, oauth_client=first)
            wrong_client = {
                **exchange,
                "client_id": second["client_id"],
                "client_secret": second["client_secret"],
            }
            assert (await client.post("/token", data=wrong_client)).status_code == 400
            token = (await client.post("/token", data=exchange)).json()
            headers = {
                "Authorization": f"Bearer {token['access_token']}",
                "Accept": "application/json, text/event-stream",
            }
            listed = (
                await client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
            ).json()
            profile_tool = next(t for t in listed["result"]["tools"] if t["name"] == "get_profile")
            assert profile_tool["_meta"]["openai/profile"] is True
            assert profile_tool["outputSchema"]["additionalProperties"] is False

            async def profile() -> dict[str, Any]:
                result = await client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "get_profile", "arguments": {}},
                    },
                )
                value: dict[str, Any] = result.json()["result"]["structuredContent"]
                return value

            assert await profile() == {"id": "123", "name": "骑友甲", "nickname": "骑友甲"}
            display_name = "新的昵称"
            assert await profile() == {"id": "123", "name": "新的昵称", "nickname": "新的昵称"}
            display_name = ""
            assert await profile() == {"id": "123"}
            refresh = {
                "grant_type": "refresh_token",
                "refresh_token": token["refresh_token"],
                "client_id": second["client_id"],
                "client_secret": second["client_secret"],
            }
            assert (await client.post("/token", data=refresh)).status_code == 400
            restarted = make_app(settings)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted), base_url=settings.origin
            ) as other:
                refresh.update(client_id=first["client_id"], client_secret=first["client_secret"])
                assert (await other.post("/token", data=refresh)).status_code == 200

    asyncio.run(run())


def test_explicit_consent_is_browser_bound(settings: Settings) -> None:
    async def run() -> None:
        app = make_app(settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=settings.origin
        ) as client:
            response = await client.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": "test-client",
                    "redirect_uri": "http://localhost:8765/callback",
                    "code_challenge": CHALLENGE,
                    "code_challenge_method": "S256",
                },
            )
            url = response.headers["location"]
            ticket = parse_qs(urlsplit(url).query)["ticket"][0]
            assert (await client.post("/connect", data={"ticket": ticket})).status_code == 400
            page = await client.get(url)
            csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)
            assert csrf
            data = {"ticket": ticket, "csrf": csrf[1]}
            assert (
                await client.post("/connect", data=data, headers={"Origin": "https://evil.test"})
            ).status_code == 400
            cookies = httpx.Cookies(client.cookies)
            client.cookies.clear()
            assert (await client.post("/connect", data=data)).status_code == 400
            client.cookies.update(cookies)
            assert (
                await client.post("/connect", data=data, headers={"Origin": settings.origin})
            ).status_code == 303
            assert (await client.post("/connect", data=data)).status_code == 400

    asyncio.run(run())
