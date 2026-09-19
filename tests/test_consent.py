"""Remember approval only for the same browser, client, destination, scopes and account."""

import asyncio
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import AnyHttpUrl
from test_integration import CHALLENGE, authorize, upstream

from xingzhe_mcp.app import create_app
from xingzhe_mcp.config import Settings
from xingzhe_mcp.oauth import SCOPE, SCOPES
from xingzhe_mcp.storage import Store


def test_remembered_consent(settings: Settings) -> None:
    settings.public_url = AnyHttpUrl("https://mcp.example.test")

    async def run() -> None:
        account = 123

        def api(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/openapi/v1/athlete/info/":
                return httpx.Response(200, json={"id": account})
            return upstream(request)

        app = create_app(settings, transport=httpx.MockTransport(api))
        store = Store(
            settings.database_url.get_secret_value(), settings.encryption_key.get_secret_value()
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=settings.origin
            ) as client,
        ):
            credentials = (
                await client.post(
                    "/register",
                    json={
                        "redirect_uris": [
                            "http://localhost:8765/callback",
                            "http://localhost:8765/other",
                        ],
                        "token_endpoint_auth_method": "client_secret_post",
                    },
                )
            ).json()

            async def start(
                scope: str = SCOPE, redirect: str = "callback", client_id: str | None = None
            ) -> httpx.Response:
                response = await client.get(
                    "/authorize",
                    params={
                        "response_type": "code",
                        "client_id": client_id or credentials["client_id"],
                        "redirect_uri": f"http://localhost:8765/{redirect}",
                        "scope": scope,
                        "state": "test-state",
                        "code_challenge": CHALLENGE,
                        "code_challenge_method": "S256",
                        "resource": settings.resource,
                    },
                )
                assert response.status_code == 302
                return await client.get(response.headers["location"])

            assert (await start()).status_code == 200
            await authorize(client, settings, oauth_client=credentials)
            cookie = next(c for c in client.cookies.jar if "xingzhe_approved_" in c.name)
            original = cookie.value
            assert original is not None
            assert cookie.name.startswith("__Host-") and cookie.secure
            assert cookie.path == "/" and cookie.has_nonstandard_attr("HttpOnly")
            assert cookie.get_nonstandard_attr("SameSite") == "lax"
            assert (await start()).status_code == 303
            # Scope elevation, a different registered callback or a different client needs consent.
            assert (await start(" ".join(SCOPES))).status_code == 200
            assert (await start(redirect="other")).status_code == 200
            assert (await start(client_id="test-client")).status_code == 200
            cookie.value = "tampered"
            assert (await start()).status_code == 200
            cookie.value = store.cipher.encrypt_at_time(
                store.cipher.decrypt(original.encode()), int(time.time()) - 31 * 86400
            ).decode()
            assert (await start()).status_code == 200
            cookie.value = original
            # Repeated confirmation bypass still requires the original browser's state cookie.
            redirect = await start()
            state = parse_qs(urlsplit(redirect.headers["location"]).query)["state"][0]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=settings.origin
            ) as other:
                assert (
                    await other.get(
                        "/oauth/xingzhe/callback", params={"state": state, "code": "code"}
                    )
                ).status_code == 400
            # Another account must not receive a grant using the previous user's consent.
            account = 456
            changed = await client.get(
                "/oauth/xingzhe/callback", params={"state": state, "code": "code"}
            )
            assert changed.status_code == 400 and "account changed" in changed.text
            async with store.transaction() as tx:
                assert await tx.get("connection", "xingzhe:456") is None
            assert (await start()).status_code == 200
            # An explicit confirmation can connect the new account and approve write access.
            await authorize(client, settings, oauth_client=credentials, scope=" ".join(SCOPES))
            assert (await start(" ".join(SCOPES))).status_code == 303
            async with store.transaction() as tx:
                await tx.disconnect("xingzhe:456")
            assert (await start()).status_code == 200

    asyncio.run(run())
