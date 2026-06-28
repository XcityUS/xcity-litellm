"""End-to-end tests for the x402 payment client (XCT-150).

Tests the full loop as an external agent would experience it:

  send request → 402 challenge → sign payment → retry with X-Payment header
  → 200 response + X-PAYMENT-RESPONSE settlement receipt

The mock_facilitator fixture intercepts the server-side facilitator call so
no real blockchain or network is needed.  The x402 client (gateway/x402_client.py)
drives the client-side protocol; mock_signer supplies fake EIP-3009 payloads.

These tests satisfy the XCT-150 acceptance criterion:
"E2E test for /api/v1/x402/pay 端到端流: send USDC request → 402 challenge
→ settle on chain → confirm receipt"
"""

import asyncio
import base64
import json
from functools import partial
from typing import Optional

import httpx
import pytest

from gateway.x402_client import (
    PaymentRequirements,
    PaymentResponse,
    X402Client,
    _encode_payment_header,
    mock_signer,
)
from tests.x402_tests.conftest import (
    PAYMENT_ADDRESS,
    PAYER_ADDRESS,
    build_payment_header,
    decode_payment_response_header,
)


# ---------------------------------------------------------------------------
# Helpers — bridge between httpx.AsyncClient and Starlette TestClient
# ---------------------------------------------------------------------------


def _make_transport(test_client):
    """Return an async httpx transport that routes through a Starlette TestClient.

    The TestClient is synchronous; we run it in a thread pool so it can be
    awaited inside an async context.
    """
    import concurrent.futures

    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    class _AsyncTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            path = str(request.url.path)
            if request.url.query:
                path = f"{path}?{request.url.query}"
            headers = dict(request.headers)
            content = request.content
            method = request.method

            loop = asyncio.get_event_loop()
            tc_resp = await loop.run_in_executor(
                _executor,
                lambda: test_client.request(method, path, headers=headers, content=content),
            )
            return httpx.Response(
                tc_resp.status_code,
                headers=dict(tc_resp.headers),
                content=tc_resp.content,
            )

    return _AsyncTransport()


class _SyncX402Client:
    """Thin synchronous wrapper around X402Client for use in non-async tests."""

    def __init__(self, signer, transport):
        self._signer = signer
        self._transport = transport

    def request_with_payment(
        self, method: str, url: str, **kwargs
    ) -> tuple[httpx.Response, Optional[PaymentResponse]]:
        signer = self._signer
        transport = self._transport

        async def _run():
            async with X402Client(signer=signer, transport=transport) as client:
                return await client.request_with_payment(method, url, **kwargs)

        return asyncio.run(_run())

    def post(self, url: str, **kwargs):
        return self.request_with_payment("POST", url, **kwargs)

    def get(self, url: str, **kwargs):
        return self.request_with_payment("GET", url, **kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def x402_client(gateway):
    """X402Client wired to the mock gateway + facilitator."""
    transport = _make_transport(gateway)
    return _SyncX402Client(signer=mock_signer, transport=transport)


@pytest.fixture()
def x402_client_with_signer(gateway):
    """Factory that returns an X402Client with a custom signer."""

    def _factory(signer):
        transport = _make_transport(gateway)
        return _SyncX402Client(signer=signer, transport=transport)

    return _factory


# ---------------------------------------------------------------------------
# 1. Happy-path E2E flow
# ---------------------------------------------------------------------------


class TestHappyPathE2E:
    """Complete x402 flow: request → 402 → sign → 200 + settlement."""

    def test_client_gets_200_on_first_paid_request(self, x402_client):
        resp, receipt = x402_client.post("http://gateway/v1/chat/completions")
        assert resp.status_code == 200

    def test_client_returns_llm_response_body(self, x402_client):
        resp, receipt = x402_client.post("http://gateway/v1/chat/completions")
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "Hello!"

    def test_client_receives_settlement_receipt(self, x402_client):
        resp, receipt = x402_client.post("http://gateway/v1/chat/completions")
        assert receipt is not None
        assert receipt.is_valid is True

    def test_settlement_receipt_has_tx_hash(self, x402_client):
        resp, receipt = x402_client.post("http://gateway/v1/chat/completions")
        assert receipt.tx_hash == "0xsettle_hash_abc123"

    def test_settlement_receipt_has_payer(self, x402_client):
        resp, receipt = x402_client.post("http://gateway/v1/chat/completions")
        assert receipt.payer == PAYER_ADDRESS

    def test_non_x402_route_returns_response_without_receipt(self, x402_client):
        resp, receipt = x402_client.get("http://gateway/health")
        assert resp.status_code == 200
        assert receipt is None

    def test_multiple_sequential_paid_requests(self, x402_client):
        for i in range(3):
            resp, receipt = x402_client.post("http://gateway/v1/chat/completions")
            assert resp.status_code == 200, f"Request {i} failed"
            assert receipt is not None and receipt.is_valid


# ---------------------------------------------------------------------------
# 2. Payment requirements parsing
# ---------------------------------------------------------------------------


class TestPaymentRequirements:
    """The client correctly extracts payment requirements from 402 responses."""

    def test_signer_receives_correct_pay_to_address(self, x402_client_with_signer):
        received: list[PaymentRequirements] = []

        async def _capturing_signer(reqs: PaymentRequirements) -> dict:
            received.append(reqs)
            return await mock_signer(reqs)

        client = x402_client_with_signer(_capturing_signer)
        client.post("http://gateway/v1/chat/completions")

        assert len(received) == 1
        assert received[0].pay_to == PAYMENT_ADDRESS

    def test_signer_receives_correct_amount(self, x402_client_with_signer):
        received: list[PaymentRequirements] = []

        async def _capturing_signer(reqs: PaymentRequirements) -> dict:
            received.append(reqs)
            return await mock_signer(reqs)

        client = x402_client_with_signer(_capturing_signer)
        client.post("http://gateway/v1/chat/completions")

        assert received[0].max_amount_required == "10000"  # 0.01 USDC atomic

    def test_signer_receives_correct_network(self, x402_client_with_signer):
        received: list[PaymentRequirements] = []

        async def _capturing_signer(reqs: PaymentRequirements) -> dict:
            received.append(reqs)
            return await mock_signer(reqs)

        client = x402_client_with_signer(_capturing_signer)
        client.post("http://gateway/v1/chat/completions")

        assert received[0].network == "eip155:8453"

    def test_signer_receives_resource_url(self, x402_client_with_signer):
        received: list[PaymentRequirements] = []

        async def _capturing_signer(reqs: PaymentRequirements) -> dict:
            received.append(reqs)
            return await mock_signer(reqs)

        client = x402_client_with_signer(_capturing_signer)
        client.post("http://gateway/v1/chat/completions")

        assert "/v1/chat/completions" in received[0].resource


# ---------------------------------------------------------------------------
# 3. Edge cases
# ---------------------------------------------------------------------------


class TestClientEdgeCases:
    """Client handles server-side rejections gracefully."""

    def test_client_returns_402_when_facilitator_rejects(
        self, x402_client, facilitator_config
    ):
        facilitator_config.is_valid = False
        facilitator_config.invalid_reason = "Signature expired"

        resp, receipt = x402_client.post("http://gateway/v1/chat/completions")
        assert resp.status_code == 402

    def test_client_returns_none_receipt_when_payment_rejected(
        self, x402_client, facilitator_config
    ):
        facilitator_config.is_valid = False
        facilitator_config.invalid_reason = "Insufficient USDC balance"

        resp, receipt = x402_client.post("http://gateway/v1/chat/completions")
        assert receipt is None

    def test_client_calls_signer_on_402(
        self, x402_client_with_signer, facilitator_config
    ):
        """Client invokes the signer exactly once when it receives a 402."""
        facilitator_config.is_valid = True  # succeeds after signing
        call_count = 0

        async def _counting_signer(reqs: PaymentRequirements) -> dict:
            nonlocal call_count
            call_count += 1
            return await mock_signer(reqs)

        client = x402_client_with_signer(_counting_signer)
        resp, _ = client.post("http://gateway/v1/chat/completions")
        assert resp.status_code == 200
        assert call_count == 1

    def test_client_handles_bearer_token_correctly(self, gateway):
        """When a real API key is supplied directly, x402 is bypassed — no payment."""
        transport = _make_transport(gateway)

        async def _should_not_be_called(reqs):
            raise RuntimeError("Signer should not be called when using Bearer token")

        async def _run():
            async with X402Client(
                signer=_should_not_be_called, transport=transport
            ) as client:
                return await client.request_with_payment(
                    "POST",
                    "http://gateway/v1/chat/completions",
                    headers={"Authorization": "Bearer sk-test-key"},
                )

        resp, receipt = asyncio.run(_run())
        assert resp.status_code == 200
        assert receipt is None


# ---------------------------------------------------------------------------
# 4. /api/v1/x402/pay endpoint E2E — the dedicated demo route (XCT-150)
# ---------------------------------------------------------------------------


class TestX402PayEndpoint:
    """E2E tests for /api/v1/x402/pay: the x402 demo/pay-per-request endpoint.

    Flow: send USDC request → 402 challenge → mock sign → confirm receipt.
    This is the explicit acceptance test described in the XCT-150 kickoff.
    """

    def test_unauthenticated_request_to_pay_endpoint_returns_402(self, gateway):
        resp = gateway.post("/api/v1/x402/pay", json={"model": "gpt-4o"})
        assert resp.status_code == 402

    def test_pay_endpoint_402_has_x402_payment_required_body(self, gateway):
        resp = gateway.post("/api/v1/x402/pay")
        body = resp.json()
        assert body["x402Version"] == 1
        assert "accepts" in body
        assert body["accepts"][0]["payTo"] == PAYMENT_ADDRESS

    def test_client_pays_and_gets_confirmation_from_pay_endpoint(self, x402_client):
        resp, receipt = x402_client.post(
            "http://gateway/api/v1/x402/pay",
            json={"model": "gpt-4o", "message": "Hello from x402"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "payment_verified"
        assert body["network"] == "eip155:8453"
        assert body["token"] == "USDC"

    def test_client_pay_endpoint_settlement_receipt_is_valid(self, x402_client):
        resp, receipt = x402_client.post(
            "http://gateway/api/v1/x402/pay",
            json={"model": "gpt-4o"},
        )
        assert receipt is not None
        assert receipt.is_valid is True
        assert receipt.tx_hash == "0xsettle_hash_abc123"

    def test_pay_endpoint_echoes_model_and_message(self, x402_client):
        resp, _ = x402_client.post(
            "http://gateway/api/v1/x402/pay",
            json={"model": "claude-3-5-sonnet", "message": "test prompt"},
        )
        body = resp.json()
        assert body["model"] == "claude-3-5-sonnet"
        assert body["message"] == "test prompt"

    def test_full_e2e_flow_summary(self, x402_client, facilitator_config):
        """Explicit trace of the complete x402 pay flow for XCT-150 demo."""
        # Step 1: unauthenticated request → middleware intercepts → 402
        # (handled transparently by X402Client)

        # Step 2+3: client signs mock payment and retries
        resp, receipt = x402_client.post(
            "http://gateway/api/v1/x402/pay",
            json={"model": "gpt-4o", "message": "Q3 demo: x402 E2E validation"},
        )

        # Step 4: confirm 200 response (payment accepted)
        assert resp.status_code == 200
        assert resp.json()["status"] == "payment_verified"

        # Step 5: confirm settlement receipt in X-PAYMENT-RESPONSE header
        assert receipt is not None
        assert receipt.is_valid is True

        # Confirm facilitator was called exactly once
        assert len(facilitator_config.received_requests) == 1


# ---------------------------------------------------------------------------
# 5. PaymentRequirements and PaymentResponse data classes
# ---------------------------------------------------------------------------


class TestDataClasses:
    """Unit tests for the client-side data-class helpers."""

    def test_payment_requirements_from_dict(self):
        d = {
            "scheme": "exact",
            "network": "eip155:8453",
            "maxAmountRequired": "10000",
            "resource": "https://api.example.com/v1/chat/completions",
            "payTo": PAYMENT_ADDRESS,
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "maxTimeoutSeconds": 300,
        }
        reqs = PaymentRequirements.from_dict(d)
        assert reqs.scheme == "exact"
        assert reqs.network == "eip155:8453"
        assert reqs.max_amount_required == "10000"
        assert reqs.pay_to == PAYMENT_ADDRESS

    def test_payment_response_from_valid_header(self):
        data = {"isValid": True, "payer": "0xPayer", "txHash": "0xhash", "invalidReason": None}
        header_val = base64.b64encode(json.dumps(data).encode()).decode()
        receipt = PaymentResponse.from_header(header_val)
        assert receipt.is_valid is True
        assert receipt.payer == "0xPayer"
        assert receipt.tx_hash == "0xhash"

    def test_payment_response_from_invalid_header(self):
        data = {"isValid": False, "payer": None, "txHash": None, "invalidReason": "Expired"}
        header_val = base64.b64encode(json.dumps(data).encode()).decode()
        receipt = PaymentResponse.from_header(header_val)
        assert receipt.is_valid is False
        assert receipt.invalid_reason == "Expired"

    def test_payment_response_from_malformed_header(self):
        receipt = PaymentResponse.from_header("!!!not-base64!!!")
        assert receipt.is_valid is False

    def test_encode_payment_header_produces_valid_base64_json(self):
        payload = {"x402Version": 1, "scheme": "exact"}
        encoded = _encode_payment_header(payload)
        decoded = json.loads(base64.b64decode(encoded))
        assert decoded == payload

    @pytest.mark.asyncio
    async def test_mock_signer_returns_valid_payload_shape(self):
        reqs = PaymentRequirements(
            scheme="exact",
            network="eip155:8453",
            max_amount_required="10000",
            resource="https://example.com/v1/chat",
            pay_to=PAYMENT_ADDRESS,
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )
        payload = await mock_signer(reqs)
        assert payload["x402Version"] == 1
        assert payload["scheme"] == "exact"
        assert "payload" in payload
        assert "signature" in payload["payload"]
        assert payload["payload"]["authorization"]["to"] == PAYMENT_ADDRESS

    @pytest.mark.asyncio
    async def test_mock_signer_uses_requirements_amount(self):
        reqs = PaymentRequirements(
            scheme="exact",
            network="eip155:8453",
            max_amount_required="50000",
            resource="https://example.com/v1/chat",
            pay_to=PAYMENT_ADDRESS,
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        )
        payload = await mock_signer(reqs)
        assert payload["payload"]["authorization"]["value"] == "50000"
