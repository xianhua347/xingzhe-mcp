"""FastAPI and stateless Streamable HTTP MCP for local and Vercel deployments."""

import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html import escape
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from psycopg import Error as DatabaseError
from pydantic import AnyHttpUrl, ValidationError
from starlette.middleware.base import RequestResponseEndpoint

from xingzhe_mcp.config import Settings
from xingzhe_mcp.oauth import SCOPE, OAuthProvider
from xingzhe_mcp.storage import Store, digest
from xingzhe_mcp.xingzhe import Activity, ActivityPage, Xingzhe, XingzheError


def create_app(
    settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    if settings is None:
        try:
            settings = Settings()
        except ValidationError:
            unconfigured = FastAPI(title="Xingzhe MCP", docs_url=None, redoc_url=None)

            @unconfigured.get("/health")
            async def unconfigured_health() -> dict[str, str]:
                return {"status": "ok"}

            @unconfigured.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
            async def missing_config(path: str) -> JSONResponse:
                return JSONResponse({"detail": "Service configuration is missing or invalid."}, 503)

            return unconfigured

    config = settings
    store = Store(config.database_url.get_secret_value(), config.encryption_key.get_secret_value())
    client = httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False)
    xingzhe = Xingzhe(config, store, client)
    provider = OAuthProvider(config, store)
    mcp: FastMCP[Any] = FastMCP(
        "Xingzhe",
        instructions="Read the owner's mainland Xingzhe activities. "
        "Dates use Unix milliseconds. Distance is meters; duration is seconds. "
        "Do not infer missing sensor measurements or undocumented units.",
        stateless_http=True,
        json_response=True,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=config.public_url,
            resource_server_url=AnyHttpUrl(config.resource),
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=False, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        transport_security=TransportSecuritySettings(
            allowed_hosts=[urlsplit(config.origin).netloc],
            allowed_origins=[config.origin],
        ),
    )
    annotations = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    )

    @mcp.tool(annotations=annotations)
    async def list_activities(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        start_timestamp: Annotated[int | None, Query(ge=0)] = None,
        end_timestamp: Annotated[int | None, Query(ge=0)] = None,
    ) -> ActivityPage:
        """List activities between Unix millisecond timestamps; page with next_offset."""
        try:
            return await xingzhe.list_activities(limit, offset, start_timestamp, end_timestamp)
        except (XingzheError, ValueError) as exc:
            raise ToolError(str(exc)) from None
        except (DatabaseError, InvalidToken):
            raise ToolError(
                "Authorization storage is unavailable. Contact the service owner."
            ) from None

    @mcp.tool(annotations=annotations)
    async def get_activity(activity_id: Annotated[int, Query(gt=0)]) -> Activity:
        """Get activity details including cadence, heart rate and power when supplied by Xingzhe."""
        try:
            return await xingzhe.get_activity(activity_id)
        except (XingzheError, ValueError) as exc:
            raise ToolError(str(exc)) from None
        except (DatabaseError, InvalidToken):
            raise ToolError(
                "Authorization storage is unavailable. Contact the service owner."
            ) from None

    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with client, mcp.session_manager.run():
            yield

    app = FastAPI(title="Xingzhe MCP", lifespan=lifespan, docs_url=None, redoc_url=None)
    callback_origins = " ".join(
        sorted({f"{uri.scheme}://{urlsplit(str(uri)).netloc}" for uri in config.mcp_redirect_uris})
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        # The SDK validates PKCE and client credentials; validate the exchange resource here.
        if request.url.path == "/token" and request.method == "POST":
            body = await request.body()
            if len(body) > 65536:
                return JSONResponse({"error": "invalid_request"}, 400)
            resources = parse_qs(body.decode(errors="replace")).get("resource", [])
            if resources and resources != [config.resource]:
                return JSONResponse(
                    {"error": "invalid_target"}, 400, headers={"Cache-Control": "no-store"}
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; "
            f"form-action 'self' https://www.imxingzhe.com {callback_origins}"
        )
        return response

    @app.exception_handler(XingzheError)
    async def upstream_error(request: Request, exc: XingzheError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, exc.status)

    async def storage_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": "Authorization storage is unavailable."}, 503)

    app.add_exception_handler(DatabaseError, storage_error)
    app.add_exception_handler(InvalidToken, storage_error)

    def form_page(title: str, action: str, *, hidden: dict[str, str] | None = None) -> HTMLResponse:
        csrf = secrets.token_urlsafe(32)
        fields = {"csrf": csrf, **(hidden or {})}
        inputs = "".join(
            f'<input type="hidden" name="{escape(k)}" value="{escape(v)}">'
            for k, v in fields.items()
        )
        response = HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{escape(title)}</title><h1>{escape(title)}</h1>"
            "<p>This deployment connects one Xingzhe account. Enter the service owner key.</p>"
            f'<form method="post" action="{escape(action)}">{inputs}'
            '<label>Owner key <input name="admin_key" type="password" required '
            'autocomplete="current-password"></label> '
            '<button type="submit">Continue</button></form></html>',
            # no-referrer makes browsers send Origin: null on native form submissions.
            # Preserve the same-origin POST origin without disclosing URLs across origins.
            headers={"Referrer-Policy": "same-origin"},
        )
        response.set_cookie(
            "xingzhe_csrf",
            csrf,
            httponly=True,
            secure=config.public_url.scheme == "https",
            samesite="lax",
            max_age=600,
        )
        return response

    async def owner_form(request: Request) -> dict[str, str]:
        form = await request.form()
        values = {key: str(value) for key, value in form.items()}
        csrf = values.get("csrf", "")
        cookie = request.cookies.get("xingzhe_csrf", "")
        if request.headers.get("origin") not in (None, config.origin):
            raise HTTPException(403, "Invalid form origin")
        if not csrf or not cookie or not secrets.compare_digest(csrf, cookie):
            raise HTTPException(403, "Form expired. Reload the page.")
        if not secrets.compare_digest(
            values.get("admin_key", ""), config.admin_key.get_secret_value()
        ):
            raise HTTPException(403, "Invalid owner key")
        return values

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        async with store.transaction() as tx:
            await tx.get("connection", "owner")
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (
            '<h1>Xingzhe MCP</h1><p><a href="/connect">Connect Xingzhe</a> · '
            '<a href="/disconnect">Disconnect and revoke MCP access</a></p>'
        )

    @app.get("/connect")
    async def connect_form() -> HTMLResponse:
        return form_page("Connect Xingzhe", "/connect")

    @app.post("/connect")
    async def connect(request: Request) -> RedirectResponse:
        await owner_form(request)
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        async with store.transaction() as tx:
            await tx.put("state", state, {"browser": digest(browser)}, expires_at=time.time() + 600)
        response = RedirectResponse(xingzhe.authorization_url(state), status_code=303)
        response.set_cookie(
            "xingzhe_oauth",
            browser,
            httponly=True,
            secure=config.public_url.scheme == "https",
            samesite="lax",
            max_age=600,
        )
        return response

    @app.get("/oauth/xingzhe/callback")
    async def callback(request: Request, state: str, code: str | None = None) -> HTMLResponse:
        async with store.transaction() as tx:
            data = await tx.get("state", state)
            browser = request.cookies.get("xingzhe_oauth", "")
            if (
                not data
                or not browser
                or not secrets.compare_digest(data["browser"], digest(browser))
            ):
                raise HTTPException(400, "Invalid or expired OAuth state")
            await tx.delete("state", state)
        if not code:
            raise HTTPException(400, "Xingzhe authorization was not granted")
        tokens = await xingzhe.exchange({"grant_type": "authorization_code", "code": code})
        # Reconnection invalidates prior grants so clients cannot silently switch owners.
        async with store.transaction() as tx:
            async with store.transaction("connection"):
                await tx.disconnect()
                await tx.put("connection", "owner", tokens.model_dump())
        response = HTMLResponse("<h1>Xingzhe connected</h1><p>Now authorize your MCP client.</p>")
        response.delete_cookie("xingzhe_oauth")
        return response

    @app.get("/consent")
    async def consent(ticket: str) -> HTMLResponse:
        async with store.transaction() as tx:
            data = await tx.get("consent", ticket)
        if data is None:
            raise HTTPException(400, "Consent expired")
        return form_page(
            "Allow your MCP client to read Xingzhe activities",
            "/consent",
            hidden={"ticket": ticket},
        )

    @app.post("/consent")
    async def approve(request: Request) -> RedirectResponse:
        values = await owner_form(request)
        async with store.transaction("connection") as tx:
            if await tx.get("connection", "owner") is None:
                raise HTTPException(
                    409, "Connect Xingzhe at /connect first, then retry authorization"
                )
        try:
            redirect = await provider.approve(values.get("ticket", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return RedirectResponse(redirect, status_code=303)

    @app.get("/disconnect")
    async def disconnect_form() -> HTMLResponse:
        return form_page("Delete stored Xingzhe tokens and revoke all MCP access", "/disconnect")

    @app.post("/disconnect")
    async def disconnect(request: Request) -> dict[str, str]:
        await owner_form(request)
        async with store.transaction() as tx:
            async with store.transaction("connection"):
                await tx.disconnect()
        return {"status": "disconnected"}

    async def require_access(request: Request) -> None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        access = await provider.load_access_token(token) if scheme.lower() == "bearer" else None
        if not access or SCOPE not in access.scopes:
            raise HTTPException(
                401,
                "A valid MCP access token is required",
                headers={
                    "WWW-Authenticate": (
                        f'Bearer resource_metadata="{config.origin}'
                        '/.well-known/oauth-protected-resource/mcp"'
                    ),
                },
            )

    @app.get("/api/activities", dependencies=[Depends(require_access)])
    async def activities(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        start_timestamp: Annotated[int | None, Query(ge=0)] = None,
        end_timestamp: Annotated[int | None, Query(ge=0)] = None,
    ) -> ActivityPage:
        try:
            return await xingzhe.list_activities(limit, offset, start_timestamp, end_timestamp)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/activities/{activity_id}", dependencies=[Depends(require_access)])
    async def activity(activity_id: int) -> Activity:
        if activity_id <= 0:
            raise HTTPException(422, "activity_id must be positive")
        return await xingzhe.get_activity(activity_id)

    app.mount("/", mcp_app)
    return app


app = create_app()
