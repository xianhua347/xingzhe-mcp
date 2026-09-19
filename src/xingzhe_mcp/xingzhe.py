"""Official Xingzhe OAuth and read-only activities API."""

import time
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from xingzhe_mcp.config import Settings
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
        if response.is_error or code not in (0, None):
            raise XingzheError("Xingzhe request failed. Try again later.")
        return data

    async def access_token(self, rejected: str | None = None) -> str:
        async with self.store.transaction("connection") as tx:
            data = await tx.get("connection", "owner")
            if data is None:
                raise XingzheError("Connect your Xingzhe account at /connect first.", 409)
            tokens = Tokens.model_validate(data)
            if tokens.expires_at < time.time() + 60 or tokens.access_token == rejected:
                tokens = await self.exchange(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": tokens.refresh_token,
                    }
                )
                await tx.put("connection", "owner", tokens.model_dump())
            return tokens.access_token

    async def request(self, path: str, params: dict[str, int] | None = None) -> dict[str, Any]:
        token = await self.access_token()
        for attempt in range(2):
            try:
                response = await self.client.get(
                    f"{BASE_URL}/openapi/v1/{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                return self._response(response)
            except XingzheError as exc:
                if exc.status != 401 or attempt:
                    raise
                token = await self.access_token(rejected=token)
            except httpx.HTTPError as exc:
                raise XingzheError("Cannot reach Xingzhe. Try again later.") from exc
        raise AssertionError("Unreachable")

    async def list_activities(
        self,
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
        data = await self.request("activities/", params)
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

    async def get_activity(self, activity_id: int) -> Activity:
        if activity_id <= 0:
            raise ValueError("activity_id must be positive")
        data = await self.request(f"activities/{activity_id}/")
        try:
            return Activity.model_validate(data["data"])
        except (KeyError, ValidationError) as exc:
            raise XingzheError("Xingzhe returned invalid activity data.") from exc
