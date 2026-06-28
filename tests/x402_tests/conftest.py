"""Shared pytest fixtures for x402 E2E tests."""

import base64
import json
import os
from typing import Iterator

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from gateway.x402_middleware import X402Middleware, _encode_b64
from tests.x402_tests.mock_facilitator import FacilitatorConfig, MockFacilitator

# ---------------------------------------------------------------------------
# Constants shared across tests
# ---------------------------------------------------------------------------

PAYMENT_ADDRESS = "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12"
PAYER_ADDRESS = "0xSenderWalletAddress000000000000000000001"
INTERNAL_KEY = "sk-x402-internal-test-key"
AMOUNT_USDC = "0.01"
CHAIN_ID = "eip155:8453"  # Base mainnet
USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_payment_header(
    signature: str = "0xdeadbeef",
    payer: str = PAYER_ADDRESS,
    recipient: str = PAYMENT_ADDRESS,
    amount: str = "10000",
    nonce: str = "0xabc123",
    valid_after: str = "0",
    valid_before: str = "9999999999",
) -> str:
    """Build a base64-encoded x402 payment header payload."""
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": CHAIN_ID,
        "payload": {
            "signature": signature,
            "authorization": {
                "from": payer,
                "to": recipient,
                "value": amount,
                "validAfter": valid_after,
                "validBefore": valid_before,
                "nonce": nonce,
            },
        },
    }
    return _encode_b64(payload)


def decode_payment_response_header(header_value: str) -> dict:
    """Decode the base64 X-PAYMENT-RESPONSE header."""
    return json.loads(base64.b64decode(header_value))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def facilitator_config() -> FacilitatorConfig:
    """Default happy-path facilitator configuration."""
    return FacilitatorConfig(
        is_valid=True,
        payer=PAYER_ADDRESS,
        tx_hash="0xsettle_hash_abc123",
    )


@pytest.fixture()
def mock_facilitator(facilitator_config: FacilitatorConfig) -> MockFacilitator:
    return MockFacilitator(facilitator_config)


def _make_gateway(
    facilitator_url: str,
    amount_usdc: str = AMOUNT_USDC,
    extra_env: dict | None = None,
) -> Starlette:
    """Build a minimal Starlette app with X402Middleware for testing."""
    env = {
        "X402_ENABLED": "true",
        "X402_PAYMENT_ADDRESS": PAYMENT_ADDRESS,
        "X402_AMOUNT_USDC": amount_usdc,
        "X402_FACILITATOR_URL": facilitator_url,
        "X402_ROUTES": "/v1/chat/completions,/api/v1/x402/pay",
        "X402_RESOURCE_URL": "https://api.test.xcity.us",
        "X402_INTERNAL_KEY": INTERNAL_KEY,
    }
    if extra_env:
        env.update(extra_env)

    for k, v in env.items():
        os.environ[k] = v

    try:

        async def _chat(request: Request) -> JSONResponse:
            return JSONResponse(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "Hello!"},
                            "finish_reason": "stop",
                            "index": 0,
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                }
            )

        async def _health(request: Request) -> JSONResponse:
            return JSONResponse({"status": "ok"})

        async def _x402_pay(request: Request) -> JSONResponse:
            body = {}
            try:
                body = await request.json()
            except Exception:
                pass
            return JSONResponse(
                {
                    "status": "payment_verified",
                    "model": body.get("model"),
                    "message": body.get("message"),
                    "network": "eip155:8453",
                    "token": "USDC",
                }
            )

        inner = Starlette(
            routes=[
                Route("/v1/chat/completions", _chat, methods=["POST"]),
                Route("/health", _health, methods=["GET"]),
                Route("/api/v1/x402/pay", _x402_pay, methods=["POST"]),
            ]
        )
        inner.add_middleware(X402Middleware)
        # Build eagerly so X402Middleware.__init__ reads env vars while they're set.
        inner.middleware_stack = inner.build_middleware_stack()
        return inner
    finally:
        for k in env:
            os.environ.pop(k, None)


@pytest.fixture()
def gateway(mock_facilitator: MockFacilitator) -> Iterator[TestClient]:
    """Full gateway + mock facilitator wired together."""
    from starlette.testclient import TestClient as _TC

    fac_client = _TC(mock_facilitator.app)

    # Patch httpx.AsyncClient.post so middleware calls go to the mock facilitator
    original_post = httpx.AsyncClient.post

    async def _patched_post(self, url, **kwargs):
        if "/verify" in url:
            # Extract just the path and post to the mock facilitator
            fac_resp = fac_client.post("/verify", json=kwargs.get("json", {}))
            return httpx.Response(
                fac_resp.status_code,
                json=fac_resp.json(),
                headers=dict(fac_resp.headers),
            )
        return await original_post(self, url, **kwargs)

    import httpx as _httpx

    _httpx.AsyncClient.post = _patched_post

    # Build the gateway using the mock facilitator's base URL (not actually used
    # since we're patching httpx directly, but it must be a valid URL)
    app = _make_gateway(facilitator_url="http://mock-facilitator.test")
    client = TestClient(app, raise_server_exceptions=True)
    yield client

    _httpx.AsyncClient.post = original_post
