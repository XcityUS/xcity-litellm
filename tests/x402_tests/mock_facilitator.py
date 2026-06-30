"""Configurable mock x402 facilitator for testing.

Exposes a minimal ASGI app that mimics the Coinbase x402 facilitator at
``https://x402.org/facilitator``.  Tests control its behaviour via the
``FacilitatorConfig`` object so that every edge-case scenario (expired
signature, insufficient balance, timeout, duplicate payment) can be exercised
without a real blockchain or network call.

Usage::

    from tests.x402_tests.mock_facilitator import MockFacilitator, FacilitatorConfig

    config = FacilitatorConfig()
    fac = MockFacilitator(config)

    # Run as an ASGI app with a test client:
    from starlette.testclient import TestClient
    client = TestClient(fac.app)
    resp = client.post("/verify", json={...})

Or start as a real HTTP server on a free port (used by conftest.py)::

    async with fac.serve() as base_url:
        ...  # base_url is "http://127.0.0.1:<port>"
"""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, Optional

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class FacilitatorConfig:
    """Controls the mock facilitator's response to verify requests."""

    # Happy-path response
    is_valid: bool = True
    payer: str = "0xSenderWalletAddress"
    tx_hash: str = "0xdeadbeef1234567890"

    # Set to a non-empty string to simulate rejection
    invalid_reason: Optional[str] = None

    # Simulate network-level failure (raise ConnectionError on /verify)
    simulate_timeout: bool = False

    # Delay before responding (seconds) — useful for timeout edge-case testing
    response_delay: float = 0.0

    # Track every request body received so tests can inspect what was sent
    received_requests: list = field(default_factory=list)

    # Seen payment signatures — enables duplicate-payment detection
    seen_signatures: Dict[str, bool] = field(default_factory=dict)
    reject_duplicates: bool = False

    def reset(self) -> None:
        self.received_requests.clear()
        self.seen_signatures.clear()
        self.is_valid = True
        self.invalid_reason = None
        self.simulate_timeout = False
        self.response_delay = 0.0
        self.reject_duplicates = False


# ---------------------------------------------------------------------------
# ASGI app
# ---------------------------------------------------------------------------


class MockFacilitator:
    """Lightweight x402-facilitator mock."""

    def __init__(self, config: Optional[FacilitatorConfig] = None) -> None:
        self.config = config or FacilitatorConfig()
        self.app = Starlette(routes=[Route("/verify", self._handle_verify, methods=["POST"])])

    async def _handle_verify(self, request: Request) -> Response:
        cfg = self.config

        if cfg.response_delay > 0:
            await asyncio.sleep(cfg.response_delay)

        body = await request.json()
        cfg.received_requests.append(body)

        # Duplicate-payment detection
        if cfg.reject_duplicates:
            sig = (
                body.get("payload", {})
                .get("payload", {})
                .get("signature", "")
            )
            if sig and sig in cfg.seen_signatures:
                return JSONResponse(
                    {
                        "isValid": False,
                        "invalidReason": "Duplicate payment: signature already processed",
                        "payer": None,
                    }
                )
            if sig:
                cfg.seen_signatures[sig] = True

        if not cfg.is_valid or cfg.invalid_reason:
            return JSONResponse(
                {
                    "isValid": False,
                    "invalidReason": cfg.invalid_reason or "Payment invalid",
                    "payer": None,
                }
            )

        return JSONResponse(
            {
                "isValid": True,
                "payer": cfg.payer,
                "txHash": cfg.tx_hash,
                "invalidReason": None,
            }
        )

    @asynccontextmanager
    async def serve(self):
        """Start the facilitator on a free port; yield ``http://host:port``."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
        server = uvicorn.Server(config)

        task = asyncio.create_task(server.serve())
        # Wait until the server is ready
        for _ in range(50):
            await asyncio.sleep(0.05)
            if server.started:
                break

        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await task
