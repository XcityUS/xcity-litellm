from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Final

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import litellm
from gateway import main
from litellm.proxy.common_request_processing import ttft_keepalive_interval
from litellm.proxy.proxy_server import app as proxy_app


@pytest.mark.asyncio
async def test_unhandled_gateway_failures_remain_readable_to_allowed_browser_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(_request: Request) -> Response:
        raise RuntimeError("synthetic gateway failure")

    monkeypatch.setattr(proxy_app.router, "routes", [Route("/cors-regression", fail)])
    origin: Final = "https://studio.xcity.ai"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app, raise_app_exceptions=False), base_url="http://gateway"
    ) as client:
        response: Final = await client.get("/cors-regression", headers={"Origin": origin})
    assert response.status_code == 500
    assert response.json() == {"error": {"message": "Internal server error", "type": "internal_server_error"}}
    assert response.headers.get("access-control-allow-origin") in {origin, "*"}


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [None, 0.0, 45.0])
async def test_gateway_enables_stream_heartbeats_without_overriding_operator_settings(
    monkeypatch: pytest.MonkeyPatch, configured: float | None
) -> None:
    @asynccontextmanager
    async def lifespan(_app: object) -> AsyncGenerator[None]:
        yield

    monkeypatch.setattr(main, "_proxy_lifespan", lifespan)
    monkeypatch.setattr(proxy_app.router, "routes", [])
    monkeypatch.setattr(litellm, "sse_keepalive_ping_interval_seconds", configured)
    async with proxy_app.router.lifespan_context(proxy_app):
        interval: Final = ttft_keepalive_interval({"stream": True})
        if configured is None:
            assert interval is not None and 0 < interval < 60
        elif configured == 0:
            assert interval is None
        else:
            assert interval == configured
        assert ttft_keepalive_interval({"stream": False}) is None
