"""FastAPI and stateless Streamable HTTP MCP for local and Vercel deployments."""

import secrets
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from html import escape
from typing import Annotated, Any, NotRequired, TypedDict
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.fernet import InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from psycopg import Error as DatabaseError
from pydantic import AnyHttpUrl, ConfigDict, Field, RootModel, ValidationError, with_config
from starlette.middleware.base import RequestResponseEndpoint

from xingzhe_mcp.config import Settings
from xingzhe_mcp.files import MAX_BASE64_LENGTH, MAX_FILE_BYTES
from xingzhe_mcp.oauth import SCOPE, SCOPES, WRITE_SCOPE, OAuthProvider
from xingzhe_mcp.storage import Store, digest
from xingzhe_mcp.xingzhe import (
    Activity,
    ActivityPage,
    RouteGPX,
    RoutePage,
    UploadPage,
    Xingzhe,
    XingzheError,
)


@with_config(ConfigDict(extra="forbid"))
class Profile(TypedDict):
    id: Annotated[str, Field(min_length=1, pattern=r"\S")]
    name: NotRequired[str]
    nickname: NotRequired[str]


class ProfileResult(RootModel[Profile]):
    pass


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
        instructions="Access the authenticated user's mainland Xingzhe activities and routes. "
        "Creating routes and uploading FIT activities require write authorization. "
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
                enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES
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

    def mcp_subject() -> str:
        access = get_access_token()
        if access is None or not access.subject:
            raise ToolError("Authentication is required. Reconnect your MCP client.")
        return access.subject

    @mcp.tool(annotations=annotations, meta={"openai/profile": True})
    async def get_profile() -> ProfileResult:
        """Return the authenticated Xingzhe account's stable ID and current display name."""
        try:
            data = await xingzhe.get_profile(mcp_subject())
            profile: Profile = {"id": data["id"]}
            if "name" in data:
                profile["name"] = data["name"]
                profile["nickname"] = data["nickname"]
            return ProfileResult(profile)
        except (XingzheError, ValueError) as exc:
            raise ToolError(str(exc)) from None
        except (DatabaseError, InvalidToken):
            raise ToolError(
                "Authorization storage is unavailable. Contact the service owner."
            ) from None

    @mcp.tool(annotations=annotations)
    async def list_activities(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        start_timestamp: Annotated[int | None, Query(ge=0)] = None,
        end_timestamp: Annotated[int | None, Query(ge=0)] = None,
    ) -> ActivityPage:
        """List activities between Unix millisecond timestamps; page with next_offset."""
        try:
            return await xingzhe.list_activities(
                mcp_subject(), limit, offset, start_timestamp, end_timestamp
            )
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
            return await xingzhe.get_activity(mcp_subject(), activity_id)
        except (XingzheError, ValueError) as exc:
            raise ToolError(str(exc)) from None
        except (DatabaseError, InvalidToken):
            raise ToolError(
                "Authorization storage is unavailable. Contact the service owner."
            ) from None

    async def checked[T](operation: Awaitable[T]) -> T:
        try:
            return await operation
        except (XingzheError, ValueError) as exc:
            raise ToolError(str(exc)) from None
        except (DatabaseError, InvalidToken):
            raise ToolError(
                "Authorization storage is unavailable. Contact the service owner."
            ) from None

    @mcp.tool(annotations=annotations)
    async def list_my_routes(
        limit: Annotated[int, Query(ge=1, le=20)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RoutePage:
        """List routes created by the authenticated account. Page using next_offset."""
        return await checked(xingzhe.list_routes(mcp_subject(), "mine", limit, offset))

    @mcp.tool(annotations=annotations)
    async def list_collected_routes(
        limit: Annotated[int, Query(ge=1, le=20)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RoutePage:
        """List routes collected by the authenticated account, including shared routes."""
        return await checked(xingzhe.list_routes(mcp_subject(), "collects", limit, offset))

    @mcp.tool(annotations=annotations)
    async def get_route(route_id: Annotated[int, Query(gt=0)]) -> dict[str, Any]:
        """Read route navigation JSON, including elevation, turns and waypoints when supplied.

        Use an ID from list_my_routes or list_collected_routes, or a route ID provided by the user.
        Xingzhe enforces route visibility; a collected or shared route may belong to someone else.
        """
        return await checked(xingzhe.get_route(mcp_subject(), route_id))

    @mcp.tool(annotations=annotations)
    async def get_route_gpx(route_id: Annotated[int, Query(gt=0)]) -> RouteGPX:
        """Read a route's GPX as UTF-8 text, up to 1,000,000 bytes. No download URL is exposed."""
        return await checked(xingzhe.get_route_gpx(mcp_subject(), route_id))

    @mcp.tool(annotations=annotations)
    async def list_uploads(
        limit: Annotated[int, Query(ge=1, le=20)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> UploadPage:
        """List the authenticated account's upload history. Check here before retrying an upload."""
        return await checked(xingzhe.list_uploads(mcp_subject(), limit, offset))

    write_annotations = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
    write_meta = {"securitySchemes": [{"type": "oauth2", "scopes": SCOPES}]}

    def write_error() -> CallToolResult | None:
        access = get_access_token()
        if access is None or WRITE_SCOPE not in access.scopes:
            return CallToolResult(
                isError=True,
                content=[
                    TextContent(
                        type="text",
                        text="Write access is required. Reconnect with xingzhe:write permission.",
                    )
                ],
                _meta={
                    "mcp/www_authenticate": [
                        f'Bearer resource_metadata="{config.origin}'
                        '/.well-known/oauth-protected-resource/mcp", '
                        f'error="insufficient_scope", scope="{SCOPE} {WRITE_SCOPE}", '
                        'error_description="Reconnect to authorize writes"'
                    ]
                },
            )
        return None

    @mcp.tool(annotations=write_annotations, meta=write_meta)
    async def create_route_from_gpx(
        title: Annotated[str, Field(min_length=1, max_length=100)],
        gpx: Annotated[str, Field(min_length=1, max_length=MAX_FILE_BYTES)],
        uuid: Annotated[str, Field(min_length=1, max_length=36)],
        distance: Annotated[float, Field(ge=0, allow_inf_nan=False)],
        sport: Annotated[int, Field(ge=1, le=4)] = 3,
        desc: Annotated[str, Field(max_length=800)] = "",
    ) -> CallToolResult:
        """Create a Xingzhe route from inline GPX XML, up to 1,000,000 UTF-8 bytes.

        This writes to the user's account. Supply a unique UUID, a title and distance in meters.
        sport: 1 walk, 2 run, 3 cycle, 4 other.
        Do not fabricate routes or retry blindly after a timeout.
        """
        if error := write_error():
            return error
        result = await checked(
            xingzhe.create_route(mcp_subject(), title, gpx, uuid, distance, sport, desc)
        )
        return CallToolResult(
            content=[TextContent(type="text", text=result.model_dump_json())],
            structuredContent=result.model_dump(mode="json"),
        )

    @mcp.tool(annotations=write_annotations, meta=write_meta)
    async def upload_activity_fit(
        title: Annotated[str, Field(min_length=1, max_length=32)],
        fit_base64: Annotated[str, Field(min_length=1, max_length=MAX_BASE64_LENGTH)],
        filename: Annotated[
            str, Field(min_length=5, max_length=256, pattern=r"^[^/\\\x00-\x1f]+\.fit$")
        ] = "activity.fit",
        detail: Annotated[str, Field(max_length=800)] = "",
        sport: Annotated[int, Field(ge=0, le=3)] = 3,
    ) -> CallToolResult:
        """Upload an actual FIT activity to Xingzhe, up to 1,000,000 decoded bytes.

        Supply standard base64 of the original FIT bytes, not a path, URL or generated workout.
        MD5 is computed by the server. sport: 0 free activity, 1 walk, 2 run, 3 cycle.
        This writes an activity. Check list_uploads before retrying an uncertain result.
        """
        if error := write_error():
            return error
        result = await checked(
            xingzhe.upload_activity(mcp_subject(), title, fit_base64, filename, detail, sport)
        )
        return CallToolResult(
            content=[TextContent(type="text", text=result.model_dump_json())],
            structuredContent=result.model_dump(mode="json"),
        )

    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with client, mcp.session_manager.run():
            yield

    app = FastAPI(title="Xingzhe MCP", lifespan=lifespan, docs_url=None, redoc_url=None)

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
            "form-action 'self' https://www.imxingzhe.com"
        )
        return response

    @app.exception_handler(XingzheError)
    async def upstream_error(request: Request, exc: XingzheError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, exc.status)

    async def storage_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": "Authorization storage is unavailable."}, 503)

    app.add_exception_handler(DatabaseError, storage_error)
    app.add_exception_handler(InvalidToken, storage_error)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        async with store.transaction() as tx:
            await tx.get("connection", "health-check")
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (
            "<h1>Xingzhe MCP</h1><p>Add this service's /mcp URL to your MCP client "
            "with OAuth. You will be redirected to Xingzhe to authorize your account.</p>"
        )

    @app.get("/connect", response_class=HTMLResponse)
    async def consent(ticket: str) -> HTMLResponse:
        browser = secrets.token_urlsafe(32)
        async with store.transaction() as tx:
            data = await tx.get("consent", ticket)
            if data is None or data.get("started"):
                raise HTTPException(400, "Authorization expired. Start again from your MCP client.")
            data["browser"] = digest(browser)
            await tx.put("consent", ticket, data, expires_at=time.time() + 600)
        permission = "读取行者昵称、运动和路书"
        if WRITE_SCOPE in data["scopes"]:
            permission += "，并创建路书、上传运动数据"
        # DCR clients choose their own names. Show the actual callback destination as well.
        response = HTMLResponse(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>连接行者账户</title><h1>连接行者账户</h1>"
            f"<p>允许 {escape(data['client_name'])} {permission}。</p>"
            f"<p>授权后返回：<code>{escape(str(data['redirect_uri']))}</code></p>"
            '<form method="post" action="/connect">'
            f'<input type="hidden" name="ticket" value="{escape(ticket, quote=True)}">'
            f'<input type="hidden" name="csrf" value="{browser}">'
            '<button type="submit">继续前往行者授权 / Continue to Xingzhe</button></form></html>'
        )
        response.set_cookie(
            f"xingzhe_consent_{digest(ticket)[:16]}",
            browser,
            httponly=True,
            secure=config.public_url.scheme == "https",
            samesite="lax",
            max_age=600,
            path="/connect",
        )
        # Keep a same-origin POST's Origin intact; no-referrer can turn it into null.
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.post("/connect")
    async def connect(request: Request) -> RedirectResponse:
        if request.headers.get("origin") not in (None, config.origin):
            raise HTTPException(400, "Invalid form origin")
        form = await request.form()
        ticket, csrf = str(form.get("ticket", "")), str(form.get("csrf", ""))
        cookie_name = f"xingzhe_consent_{digest(ticket)[:16]}"
        browser_cookie = request.cookies.get(cookie_name, "")
        state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        async with store.transaction() as tx:
            data = await tx.get("consent", ticket)
            if (
                not data
                or data.get("started")
                or not csrf
                or not browser_cookie
                or not secrets.compare_digest(csrf, browser_cookie)
                or not secrets.compare_digest(data.get("browser", ""), digest(csrf))
            ):
                raise HTTPException(
                    400, "Invalid or expired consent. Restart from your MCP client."
                )
            data["started"] = True
            await tx.put("consent", ticket, data, expires_at=time.time() + 600)
            await tx.put(
                "state",
                state,
                {
                    "browser": digest(browser),
                    "ticket": ticket,
                    "upstream_scope": "write" if WRITE_SCOPE in data["scopes"] else "read",
                },
                expires_at=time.time() + 600,
            )
        response = RedirectResponse(
            xingzhe.authorization_url(state, write=WRITE_SCOPE in data["scopes"]), status_code=303
        )
        response.delete_cookie(cookie_name, path="/connect")
        response.set_cookie(
            f"xingzhe_oauth_{digest(state)[:16]}",
            browser,
            httponly=True,
            secure=config.public_url.scheme == "https",
            samesite="lax",
            max_age=600,
            path="/oauth/xingzhe/callback",
        )
        return response

    @app.get("/oauth/xingzhe/callback")
    async def callback(request: Request, state: str, code: str | None = None) -> RedirectResponse:
        cookie_name = f"xingzhe_oauth_{digest(state)[:16]}"
        async with store.transaction() as tx:
            data = await tx.get("state", state)
            browser = request.cookies.get(cookie_name, "")
            if (
                not data
                or not browser
                or not secrets.compare_digest(data["browser"], digest(browser))
            ):
                raise HTTPException(400, "Invalid or expired OAuth state")
            await tx.delete("state", state)
        if not code:
            async with store.transaction() as tx:
                await tx.delete("consent", data["ticket"])
            raise HTTPException(
                400, "Xingzhe authorization was not granted. Restart from your MCP client."
            )
        tokens = await xingzhe.exchange(
            {"grant_type": "authorization_code", "code": code}, scope=data["upstream_scope"]
        )
        subject = await xingzhe.account_id(tokens.access_token)
        try:
            redirect = await provider.approve(data["ticket"], subject, tokens)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        response = RedirectResponse(redirect, status_code=303)
        response.delete_cookie(cookie_name, path="/oauth/xingzhe/callback")
        return response

    async def require_access(request: Request) -> str:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        access = await provider.load_access_token(token) if scheme.lower() == "bearer" else None
        if not access or not access.subject or SCOPE not in access.scopes:
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

        return access.subject

    @app.post("/disconnect")
    async def disconnect(subject: Annotated[str, Depends(require_access)]) -> dict[str, str]:
        async with store.transaction() as tx:
            await tx.lock(f"connection:{subject}")
            await tx.disconnect(subject)
        return {"status": "disconnected"}

    @app.get("/api/activities")
    async def activities(
        subject: Annotated[str, Depends(require_access)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
        start_timestamp: Annotated[int | None, Query(ge=0)] = None,
        end_timestamp: Annotated[int | None, Query(ge=0)] = None,
    ) -> ActivityPage:
        try:
            return await xingzhe.list_activities(
                subject, limit, offset, start_timestamp, end_timestamp
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/activities/{activity_id}")
    async def activity(
        activity_id: int, subject: Annotated[str, Depends(require_access)]
    ) -> Activity:
        if activity_id <= 0:
            raise HTTPException(422, "activity_id must be positive")
        return await xingzhe.get_activity(subject, activity_id)

    app.mount("/", mcp_app)
    return app


app = create_app()
