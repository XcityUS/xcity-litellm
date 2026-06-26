"""Unit tests for the x402 pay-per-request middleware.

Tests are written for pytest and do not need a running LLM backend or real
blockchain; the facilitator HTTP call is patched with httpx.MockTransport.
"""

import base64
import json
import os

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from gateway.x402_middleware import (
    X402Middleware,
    _decode_payment_header,
    _encode_b64,
    _usdc_to_atomic,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PAYMENT_ADDRESS = "0xDeAdBeEf000000000000000000000000000000Ab"
_FACILITATOR_VERIFY_URL = "https://x402.org/facilitator/verify"


def _make_app(env_overrides: dict) -> Starlette:
    """Return a minimal Starlette app with X402Middleware applied."""
    os.environ.update(env_overrides)
    try:

        async def _hello(request: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        inner = Starlette(routes=[])
        inner.add_route("/v1/chat/completions", _hello, methods=["POST"])
        inner.add_route("/health", _hello, methods=["GET"])
        inner.add_middleware(X402Middleware)
        return inner
    finally:
        for k in env_overrides:
            os.environ.pop(k, None)


def _enabled_env() -> dict:
    return {
        "X402_ENABLED": "true",
        "X402_PAYMENT_ADDRESS": _PAYMENT_ADDRESS,
        "X402_AMOUNT_USDC": "0.01",
        "X402_FACILITATOR_URL": "https://x402.org/facilitator",
        "X402_ROUTES": "/v1/chat/completions",
        "X402_RESOURCE_URL": "https://api.example.com",
    }


def _sample_payment_payload() -> dict:
    return {
        "x402Version": 1,
        "scheme": "exact",
        "network": "eip155:8453",
        "payload": {
            "signature": "0xdeadbeef",
            "authorization": {
                "from": "0xSender",
                "to": _PAYMENT_ADDRESS,
                "value": "10000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0xabc",
            },
        },
    }


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------


class TestUsdcToAtomic:
    def test_whole_dollar(self):
        assert _usdc_to_atomic("1") == "1000000"

    def test_fractional(self):
        assert _usdc_to_atomic("0.01") == "10000"

    def test_tiny(self):
        assert _usdc_to_atomic("0.000001") == "1"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _usdc_to_atomic("not-a-number")


class TestDecodePaymentHeader:
    def test_json_payload(self):
        payload = {"x402Version": 1, "scheme": "exact"}
        raw = json.dumps(payload)
        assert _decode_payment_header(raw) == payload

    def test_base64_payload(self):
        payload = {"x402Version": 1, "scheme": "exact"}
        raw = base64.b64encode(json.dumps(payload).encode()).decode()
        assert _decode_payment_header(raw) == payload

    def test_invalid_raises(self):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            _decode_payment_header("not-json-or-b64!!!")


# ---------------------------------------------------------------------------
# Integration tests — middleware behaviour
# ---------------------------------------------------------------------------


class TestMiddlewareDisabled:
    """When X402_ENABLED is not 'true', the middleware is a pass-through."""

    def setup_method(self):
        self.app = _make_app({"X402_ENABLED": "false"})
        self.client = TestClient(self.app)

    def test_request_passes_through(self):
        resp = self.client.post("/v1/chat/completions")
        assert resp.status_code == 200
        assert resp.text == "ok"

    def test_non_x402_route_passes_through(self):
        resp = self.client.get("/health")
        assert resp.status_code == 200


class TestMiddlewareEnabled:
    """With X402_ENABLED=true, unauthenticated requests to x402 routes → 402."""

    def setup_method(self):
        self.app = _make_app(_enabled_env())
        self.client = TestClient(self.app)

    def test_no_auth_returns_402(self):
        resp = self.client.post("/v1/chat/completions")
        assert resp.status_code == 402

    def test_402_body_has_required_fields(self):
        resp = self.client.post("/v1/chat/completions")
        body = resp.json()
        assert body["x402Version"] == 1
        assert "accepts" in body
        assert len(body["accepts"]) == 1
        accepts = body["accepts"][0]
        assert accepts["scheme"] == "exact"
        assert accepts["network"] == "eip155:8453"
        assert accepts["payTo"] == _PAYMENT_ADDRESS
        assert accepts["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        assert accepts["maxAmountRequired"] == "10000"  # 0.01 USDC

    def test_402_body_has_resource_url(self):
        resp = self.client.post("/v1/chat/completions")
        accepts = resp.json()["accepts"][0]
        assert accepts["resource"] == "https://api.example.com/v1/chat/completions"

    def test_402_response_header_is_base64_json(self):
        resp = self.client.post("/v1/chat/completions")
        header_val = resp.headers.get("x-payment-response")
        assert header_val is not None
        decoded = json.loads(base64.b64decode(header_val))
        assert decoded["x402Version"] == 1

    def test_api_key_skips_x402(self):
        """Bearer token in Authorization bypasses x402 check."""
        resp = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-test"},
        )
        # The inner app returns 200 "ok"
        assert resp.status_code == 200

    def test_non_x402_route_passes_through(self):
        resp = self.client.get("/health")
        assert resp.status_code == 200

    def test_malformed_payment_header_returns_400(self):
        resp = self.client.post(
            "/v1/chat/completions",
            headers={"X-Payment": "!!!not-valid-json-or-base64!!!"},
        )
        assert resp.status_code == 400

    def test_valid_payment_calls_facilitator(self, monkeypatch):
        """A valid X-Payment header should reach the facilitator and pass through."""
        payload = _sample_payment_payload()
        payment_header = _encode_b64(payload)

        verify_response = httpx.Response(
            200,
            json={"isValid": True, "payer": "0xSender", "invalidReason": None},
        )

        async def _mock_post(self_client, url, **kwargs):
            assert "/verify" in url
            return verify_response

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)

        resp = self.client.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert resp.status_code == 200

    def test_invalid_payment_returns_402(self, monkeypatch):
        """A payment that the facilitator rejects → 402."""
        payload = _sample_payment_payload()
        payment_header = _encode_b64(payload)

        verify_response = httpx.Response(
            200,
            json={
                "isValid": False,
                "invalidReason": "Signature expired",
                "payer": None,
            },
        )

        async def _mock_post(self_client, url, **kwargs):
            return verify_response

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)

        resp = self.client.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert resp.status_code == 402
        assert "Signature expired" in resp.json()["error"]

    def test_facilitator_unreachable_returns_402(self, monkeypatch):
        payload = _sample_payment_payload()
        payment_header = _encode_b64(payload)

        async def _mock_post(self_client, url, **kwargs):
            raise httpx.ConnectError("Connection refused")

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)

        resp = self.client.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert resp.status_code == 402
        assert "Facilitator unreachable" in resp.json()["error"]

    def test_settlement_header_on_successful_payment(self, monkeypatch):
        """After a successful payment the upstream response gets X-PAYMENT-RESPONSE."""
        payload = _sample_payment_payload()
        payment_header = _encode_b64(payload)
        settle_data = {
            "isValid": True,
            "payer": "0xSender",
            "txHash": "0xabc123",
            "invalidReason": None,
        }
        verify_response = httpx.Response(200, json=settle_data)

        async def _mock_post(self_client, url, **kwargs):
            return verify_response

        monkeypatch.setattr(httpx.AsyncClient, "post", _mock_post)

        resp = self.client.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert resp.status_code == 200
        settlement_header = resp.headers.get("x-payment-response")
        assert settlement_header is not None
        decoded = json.loads(base64.b64decode(settlement_header))
        assert decoded["isValid"] is True


class TestMiddlewareConfigValidation:
    def test_missing_payment_address_raises(self):
        os.environ["X402_ENABLED"] = "true"
        os.environ.pop("X402_PAYMENT_ADDRESS", None)
        try:
            with pytest.raises(RuntimeError, match="X402_PAYMENT_ADDRESS"):
                X402Middleware(app=None)
        finally:
            os.environ.pop("X402_ENABLED", None)
