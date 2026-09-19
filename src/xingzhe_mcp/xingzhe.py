"""Official Xingzhe OAuth, activities, routes and uploads."""

import time
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from xingzhe_mcp.config import Settings
from xingzhe_mcp.files import MAX_FILE_BYTES, gpx_bytes
from xingzhe_mcp.storage import Store

BASE_URL = "https://www.imxingzhe.com"


class XingzheError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class Tokens(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: float


class Activity(BaseModel):
    """Preserve additional upstream fields without guessing undocumented units."""

    model_config = ConfigDict(extra="allow")
    id: int
    title: str | None = None
    distance: float | None = Field(default=None, description="Distance in meters")
    duration: float | None = Field(default=None, description="Duration in seconds")
    avg_cadence: float | None = None
    max_cadence: float | None = None


class ActivityPage(BaseModel):
    count: int
    next_offset: int | None
    results: list[Activity]


StreamField = Literal[
    "timestamp",
    "location",
    "cadence",
    "speed",
    "distance",
    "heartrate",
    "altitude",
    "power",
    "temperature",
    "left_balance",
    "right_balance",
]


class ActivityStreamPage(BaseModel):
    activity_id: int
    total_points: int
    offset: int
    next_offset: int | None
    available_streams: list[str]
    unavailable_streams: list[str]
    streams: dict[str, list[JsonValue]]


class Route(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    title: str | None = None
    distance: float | None = None
    sport: int | None = None
    desc: str | None = None


class RoutePage(BaseModel):
    count: int
    next_offset: int | None
    results: list[Route]


class Upload(BaseModel):
    model_config = ConfigDict(extra="allow")
    upload_id: int
    workout_id: int | None = None
    new: bool | None = None
    credits: float | None = None
    uuid: str | None = None


class UploadPage(BaseModel):
    count: int
    next_offset: int | None
    results: list[Upload]


class RouteGPX(BaseModel):
    route_id: int
    filename: str
    gpx: str


class Xingzhe:
    def __init__(self, settings: Settings, store: Store, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.store = store
        self.client = client

    def authorization_url(self, state: str) -> str:
        return f"{BASE_URL}/oauth2/v2/authorize?" + urlencode(
            {
                "client_id": self.settings.xingzhe_client_id,
                "response_type": "code",
                "scope": "read",
                "state": state,
                "redirect_uri": f"{self.settings.origin}/oauth/xingzhe/callback",
            }
        )

    async def exchange(self, fields: dict[str, str]) -> Tokens:
        try:
            response = await self.client.post(
                f"{BASE_URL}/oauth2/v2/access_token/",
                headers={
                    "Authorization": "Bearer "
                    + self.settings.xingzhe_client_id
                    + ":"
                    + self.settings.xingzhe_client_secret.get_secret_value()
                },
                files={key: (None, value) for key, value in fields.items()},
            )
            data = self._response(response)
            expiry = data.get("expires_at")
            if expiry is None:
                expiry = time.time() + float(data["expires_in"])
            return Tokens.model_validate(
                dict(
                    access_token=data["access_token"],
                    refresh_token=data.get("refresh_token") or fields.get("refresh_token"),
                    expires_at=float(expiry),
                )
            )
        except (KeyError, TypeError, ValueError, httpx.HTTPError) as exc:
            raise XingzheError("Xingzhe token exchange failed. Reconnect your account.") from exc

    @staticmethod
    def _response(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise XingzheError("Xingzhe returned an invalid response.") from exc
        if not isinstance(data, dict):
            raise XingzheError("Xingzhe returned an invalid response.")
        code = data.get("code", 0)
        if response.status_code == 401 or code == 401:
            raise XingzheError("Xingzhe authorization expired. Reconnect your account.", 401)
        if response.status_code == 429 or (code == 400 and data.get("msg") == "API Limited"):
            raise XingzheError("Xingzhe rate limit reached. Try again later.", 429)
        if response.is_error or code not in (0, 200, None):
            raise XingzheError("Xingzhe request failed. Try again later.")
        return data

    @staticmethod
    def profile(data: dict[str, Any]) -> dict[str, str]:
        profile = data.get("data", data)
        user_id = profile.get("id") if isinstance(profile, dict) else None
        if type(user_id) is not int or user_id <= 0:
            raise XingzheError("Xingzhe returned an invalid account identity.")
        result = {"id": str(user_id)}
        name = profile.get("username")
        if isinstance(name, str) and name.strip():
            result.update(name=name.strip(), nickname=name.strip())
        return result

    async def account_id(self, token: str) -> str:
        """Resolve identity using the authenticated upstream profile, never client input."""
        try:
            response = await self.client.get(
                f"{BASE_URL}/openapi/v1/athlete/info/",
                headers={"Authorization": f"Bearer {token}"},
            )
            return "xingzhe:" + self.profile(self._response(response))["id"]
        except httpx.HTTPError as exc:
            raise XingzheError("Cannot verify your Xingzhe account. Try again.") from exc

    async def get_profile(self, subject: str) -> dict[str, str]:
        profile = self.profile(await self.request(subject, "athlete/info/"))
        if "xingzhe:" + profile["id"] != subject:
            raise XingzheError("Xingzhe account identity changed. Reconnect your account.", 403)
        return profile

    async def access_token(self, subject: str, rejected: str | None = None) -> str:
        async with self.store.transaction(f"connection:{subject}") as tx:
            data = await tx.get("connection", subject)
            if data is None:
                raise XingzheError("Reconnect your Xingzhe account from your MCP client.", 409)
            tokens = Tokens.model_validate(data)
            if tokens.expires_at < time.time() + 60 or tokens.access_token == rejected:
                tokens = await self.exchange(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": tokens.refresh_token,
                    },
                )
                await tx.put("connection", subject, tokens.model_dump(), subject=subject)
            return tokens.access_token

    async def response(
        self,
        subject: str,
        path: str,
        params: dict[str, int] | None = None,
        *,
        max_bytes: int = 2_000_000,
        read_via_post: bool = False,
    ) -> httpx.Response:
        token = await self.access_token(subject)
        for attempt in range(2):
            try:
                async with self.client.stream(
                    "POST" if read_via_post else "GET",
                    f"{BASE_URL}/openapi/v1/{path}",
                    params=params,
                    json={} if read_via_post else None,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15,
                ) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise XingzheError(
                                "Xingzhe response exceeds the inline MCP size limit."
                            )
                    result = httpx.Response(
                        response.status_code,
                        headers={"content-type": response.headers.get("content-type", "")},
                        content=bytes(body),
                    )
                if result.status_code == 401 or "json" in result.headers.get("content-type", ""):
                    self._response(result)
                elif result.is_error or result.is_redirect:
                    raise XingzheError(
                        "Xingzhe request failed or returned an unsupported redirect.",
                        404 if result.status_code == 404 else 502,
                    )
                return result
            except XingzheError as exc:
                if exc.status != 401 or attempt:
                    raise
                token = await self.access_token(subject, rejected=token)
            except httpx.HTTPError as exc:
                raise XingzheError("Cannot reach Xingzhe. Try again later.") from exc
        raise AssertionError("Unreachable")

    async def request(
        self,
        subject: str,
        path: str,
        params: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return self._response(await self.response(subject, path, params))

    async def list_routes(
        self,
        subject: str,
        collection: Literal["mine", "collects"],
        limit: int = 20,
        offset: int = 0,
    ) -> RoutePage:
        if not 1 <= limit <= 20 or offset < 0:
            raise ValueError("limit must be 1–20 and offset must be nonnegative")
        data = await self.request(
            subject, f"routes/{collection}/", {"limit": limit, "offset": offset}
        )
        try:
            routes = [Route.model_validate(item) for item in data["results"]]
            count = int(data["count"])
            end = offset + len(routes)
            return RoutePage(
                count=count, results=routes, next_offset=end if routes and end < count else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise XingzheError("Xingzhe returned invalid route data.") from exc

    async def get_route(self, subject: str, route_id: int) -> dict[str, Any]:
        if route_id <= 0:
            raise ValueError("route_id must be positive")
        data = await self.request(subject, f"routes/{route_id}/pro/")
        result = data.get("data", data)
        if not isinstance(result, dict) or result.get("id") != route_id:
            raise XingzheError("Xingzhe returned invalid navigation data.")
        return result

    async def get_route_gpx(self, subject: str, route_id: int) -> RouteGPX:
        if route_id <= 0:
            raise ValueError("route_id must be positive")
        response = await self.response(subject, f"routes/{route_id}/gpx/", max_bytes=MAX_FILE_BYTES)
        try:
            content = response.content.decode("utf-8-sig")
            gpx_bytes(content)
        except ValueError as exc:
            raise XingzheError("Xingzhe returned invalid UTF-8 GPX data.") from exc
        return RouteGPX(route_id=route_id, filename=f"route-{route_id}.gpx", gpx=content)

    async def list_uploads(self, subject: str, limit: int = 20, offset: int = 0) -> UploadPage:
        if not 1 <= limit <= 20 or offset < 0:
            raise ValueError("limit must be 1–20 and offset must be nonnegative")
        data = await self.request(subject, "uploads/", {"limit": limit, "offset": offset})
        try:
            uploads = [Upload.model_validate(item) for item in data["results"]]
            count = int(data["count"])
            end = offset + len(uploads)
            return UploadPage(
                count=count, results=uploads, next_offset=end if uploads and end < count else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise XingzheError("Xingzhe returned invalid upload data.") from exc

    async def list_activities(
        self,
        subject: str,
        limit: int = 20,
        offset: int = 0,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
    ) -> ActivityPage:
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("limit must be 1–100 and offset must be nonnegative")
        if any(value is not None and value < 0 for value in (start_timestamp, end_timestamp)):
            raise ValueError("Timestamps must be nonnegative Unix milliseconds")
        if start_timestamp is not None and end_timestamp is not None:
            if start_timestamp > end_timestamp:
                raise ValueError("start_timestamp must not exceed end_timestamp")
        params = {"limit": limit, "offset": offset}
        if start_timestamp is not None:
            params["start_timestamp"] = start_timestamp
        if end_timestamp is not None:
            params["end_timestamp"] = end_timestamp
        data = await self.request(subject, "activities/", params)
        try:
            activities = [Activity.model_validate(item) for item in data["results"]]
            count = int(data["count"])
            next_offset = offset + len(activities)
            return ActivityPage(
                count=count,
                results=activities,
                next_offset=next_offset if activities and next_offset < count else None,
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise XingzheError("Xingzhe returned invalid activity data.") from exc

    async def get_activity(self, subject: str, activity_id: int) -> Activity:
        if activity_id <= 0:
            raise ValueError("activity_id must be positive")
        data = await self.request(subject, f"activities/{activity_id}/")
        try:
            activity = data["data"]
            if not isinstance(activity, dict) or type(activity.get("user_id")) is not int:
                raise XingzheError("Xingzhe returned an activity without a valid owner.")
            if f"xingzhe:{activity['user_id']}" != subject:
                raise XingzheError("Activity does not belong to your Xingzhe account.", 403)
            return Activity.model_validate(activity)
        except (KeyError, ValidationError) as exc:
            raise XingzheError("Xingzhe returned invalid activity data.") from exc

    async def get_activity_stream(
        self,
        subject: str,
        activity_id: int,
        fields: list[StreamField] | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> ActivityStreamPage:
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("limit must be 1–1000 and offset must be nonnegative")
        # Check ownership before requesting any location or sensor samples.
        await self.get_activity(subject, activity_id)
        response = await self.response(
            subject,
            f"activities/{activity_id}/stream/",
            read_via_post=True,
            max_bytes=20_000_000,
        )
        data = self._response(response).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("timestamp"), list):
            raise XingzheError("Xingzhe returned invalid activity stream data.")
        count = len(data["timestamp"])
        if any(not isinstance(v, list) or len(v) not in (0, count) for v in data.values()):
            raise XingzheError("Xingzhe returned misaligned activity streams.")
        selected = list(dict.fromkeys(["timestamp", *(fields if fields is not None else data)]))
        end = min(offset + limit, count)
        try:
            return ActivityStreamPage(
                activity_id=activity_id,
                total_points=count,
                offset=offset,
                next_offset=end if end < count else None,
                available_streams=[key for key, values in data.items() if values],
                unavailable_streams=[key for key in selected if not data.get(key)],
                streams={key: data.get(key, [])[offset:end] for key in selected},
            )
        except ValidationError as exc:
            raise XingzheError("Xingzhe returned invalid activity stream values.") from exc
