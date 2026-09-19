"""FastAPI application, shared by local and serverless deployments."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Process liveness, not upstream API connectivity."""

    status: Literal["ok"] = "ok"


app = FastAPI(
    title="Xingzhe MCP",
    description="Self-hosted access to mainland Xingzhe cycling data. Under development.",
)


@app.get("/health", tags=["health"])
async def health() -> HealthResponse:
    """Check that the service is running without contacting Xingzhe."""
    return HealthResponse()
