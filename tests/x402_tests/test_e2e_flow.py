"""End-to-end tests for the x402 payment flow.

These tests exercise the complete HTTP-level payment loop:

  1. Client sends request without auth → 402 + PaymentRequired body
  2. Client extracts payment requirements, builds an X-PAYMENT header
  3. Client re-sends the signed request
  4. Mock facilitator validates the payment → 200 + X-PAYMENT-RESPONSE header
  5. (Edge cases) Facilitator rejects → 402 with a reason

All tests are isolated from the real blockchain and Coinbase facilitator;
every facilitator call is intercepted by the MockFacilitator fixture defined
in conftest.py.
"""

import base64
import json

import pytest

from tests.x402_tests.conftest import (
    PAYMENT_ADDRESS,
    PAYER_ADDRESS,
    build_payment_header,
    decode_payment_response_header,
)


# ---------------------------------------------------------------------------
# 1. Happy path — request → 402 → sign → resend → 200 → settle
# ---------------------------------------------------------------------------


class TestHappyPath:
    """The canonical three-step x402 flow works end-to-end."""

    def test_step1_unauthenticated_request_returns_402(self, gateway):
        resp = gateway.post("/v1/chat/completions")
        assert resp.status_code == 402

    def test_step1_402_body_is_valid_x402_payment_required(self, gateway):
        resp = gateway.post("/v1/chat/completions")
        body = resp.json()
        assert body["x402Version"] == 1
        assert isinstance(body["accepts"], list)
        assert len(body["accepts"]) >= 1

    def test_step1_402_body_accepts_field_has_correct_payment_details(self, gateway):
        resp = gateway.post("/v1/chat/completions")
        accept = resp.json()["accepts"][0]
        assert accept["scheme"] == "exact"
        assert accept["network"] == "eip155:8453"
        assert accept["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        assert accept["payTo"] == PAYMENT_ADDRESS
        assert accept["maxAmountRequired"] == "10000"  # 0.01 USDC in atomic units

    def test_step1_402_body_resource_url_contains_path(self, gateway):
        resp = gateway.post("/v1/chat/completions")
        accept = resp.json()["accepts"][0]
        assert "/v1/chat/completions" in accept["resource"]

    def test_step1_402_response_has_x_payment_response_header(self, gateway):
        resp = gateway.post("/v1/chat/completions")
        assert "x-payment-response" in resp.headers
        # Header must be valid base64 JSON
        decoded = decode_payment_response_header(resp.headers["x-payment-response"])
        assert decoded["x402Version"] == 1

    def test_step2_payment_header_is_readable(self, gateway):
        """Demonstrates that a client can decode the 402 body and build a payment header."""
        resp_402 = gateway.post("/v1/chat/completions")
        accept = resp_402.json()["accepts"][0]
        # Client builds payment header using the requirements from 402
        payment_header = build_payment_header(
            recipient=accept["payTo"],
            amount=accept["maxAmountRequired"],
        )
        decoded = json.loads(base64.b64decode(payment_header))
        assert decoded["x402Version"] == 1

    def test_step3_valid_payment_header_returns_200(self, gateway):
        payment_header = build_payment_header()
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert resp.status_code == 200

    def test_step3_successful_payment_returns_llm_response_body(self, gateway):
        payment_header = build_payment_header()
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "Hello!"

    def test_step3_successful_payment_response_has_settlement_header(self, gateway):
        payment_header = build_payment_header()
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert "x-payment-response" in resp.headers
        settlement = decode_payment_response_header(resp.headers["x-payment-response"])
        assert settlement["isValid"] is True

    def test_step3_settlement_header_contains_tx_hash(self, gateway):
        payment_header = build_payment_header()
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        settlement = decode_payment_response_header(resp.headers["x-payment-response"])
        assert settlement.get("txHash") == "0xsettle_hash_abc123"

    def test_step3_settlement_header_contains_payer(self, gateway):
        payment_header = build_payment_header()
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        settlement = decode_payment_response_header(resp.headers["x-payment-response"])
        assert settlement.get("payer") == PAYER_ADDRESS

    def test_facilitator_verify_request_body_is_correct(self, gateway, facilitator_config):
        payment_header = build_payment_header(signature="0xsig_check")
        gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert len(facilitator_config.received_requests) == 1
        verify_body = facilitator_config.received_requests[0]
        assert verify_body["x402Version"] == 1
        assert "payload" in verify_body
        assert "paymentRequirements" in verify_body

    def test_facilitator_verify_payment_requirements_match_402(self, gateway, facilitator_config):
        payment_header = build_payment_header()
        gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        reqs = facilitator_config.received_requests[0]["paymentRequirements"]
        assert reqs["payTo"] == PAYMENT_ADDRESS
        assert reqs["network"] == "eip155:8453"
        assert reqs["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


# ---------------------------------------------------------------------------
# 2. API-key bypass — existing Bearer token skips x402 entirely
# ---------------------------------------------------------------------------


class TestApiKeyBypass:
    """Requests with a valid Bearer token skip the x402 payment gate."""

    def test_bearer_token_bypasses_402_gate(self, gateway):
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-some-api-key"},
        )
        # The inner app returns 200 regardless; no payment needed
        assert resp.status_code == 200

    def test_bearer_token_bypass_does_not_call_facilitator(self, gateway, facilitator_config):
        gateway.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-some-api-key"},
        )
        assert len(facilitator_config.received_requests) == 0

    def test_non_x402_route_is_always_pass_through(self, gateway):
        resp = gateway.get("/health")
        assert resp.status_code == 200

    def test_non_x402_route_does_not_call_facilitator(self, gateway, facilitator_config):
        gateway.get("/health")
        assert len(facilitator_config.received_requests) == 0


# ---------------------------------------------------------------------------
# 3. Edge cases
# ---------------------------------------------------------------------------


class TestExpiredSignature:
    """Facilitator rejects payment because the EIP-3009 authorization is expired."""

    def test_expired_signature_returns_402(self, gateway, facilitator_config):
        facilitator_config.is_valid = False
        facilitator_config.invalid_reason = "Authorization expired: validBefore is in the past"

        payment_header = build_payment_header(
            valid_before="1000000000"  # epoch far in the past
        )
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert resp.status_code == 402

    def test_expired_signature_error_is_in_response_body(self, gateway, facilitator_config):
        facilitator_config.is_valid = False
        facilitator_config.invalid_reason = "Authorization expired: validBefore is in the past"

        payment_header = build_payment_header(valid_before="1000000000")
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        body = resp.json()
        assert "expired" in body["error"].lower()

    def test_expired_signature_still_returns_accepts_block(self, gateway, facilitator_config):
        facilitator_config.is_valid = False
        facilitator_config.invalid_reason = "Authorization expired"

        payment_header = build_payment_header(valid_before="1000000000")
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        body = resp.json()
        # Client can immediately retry with a fresh payment
        assert "accepts" in body
        assert len(body["accepts"]) >= 1


class TestInsufficientBalance:
    """Facilitator rejects payment because the payer's USDC balance is too low."""

    def test_insufficient_balance_returns_402(self, gateway, facilitator_config):
        facilitator_config.is_valid = False
        facilitator_config.invalid_reason = "Insufficient USDC balance"

        payment_header = build_payment_header(amount="1")  # 0.000001 USDC — too small
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert resp.status_code == 402

    def test_insufficient_balance_error_message(self, gateway, facilitator_config):
        facilitator_config.is_valid = False
        facilitator_config.invalid_reason = "Insufficient USDC balance"

        payment_header = build_payment_header(amount="1")
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert "Insufficient" in resp.json()["error"]

    def test_correct_amount_after_rejection_succeeds(self, gateway, facilitator_config):
        """Client can retry with the correct amount from the accepts block."""
        # First request — fails
        facilitator_config.is_valid = False
        facilitator_config.invalid_reason = "Insufficient USDC balance"
        resp_fail = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": build_payment_header(amount="1")},
        )
        assert resp_fail.status_code == 402

        # Fix balance and retry
        facilitator_config.is_valid = True
        facilitator_config.invalid_reason = None
        resp_ok = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": build_payment_header()},  # correct amount
        )
        assert resp_ok.status_code == 200


class TestFacilitatorTimeout:
    """Facilitator is unreachable — treated as a rejected payment."""

    def test_unreachable_facilitator_returns_402(self, gateway, facilitator_config):
        import httpx as _httpx

        original_post = _httpx.AsyncClient.post

        async def _timeout(self, url, **kwargs):
            raise _httpx.ConnectError("Connection refused")

        _httpx.AsyncClient.post = _timeout
        try:
            payment_header = build_payment_header()
            resp = gateway.post(
                "/v1/chat/completions",
                headers={"X-Payment": payment_header},
            )
            assert resp.status_code == 402
        finally:
            _httpx.AsyncClient.post = original_post

    def test_unreachable_facilitator_error_message(self, gateway, facilitator_config):
        import httpx as _httpx

        original_post = _httpx.AsyncClient.post

        async def _timeout(self, url, **kwargs):
            raise _httpx.ConnectError("Connection refused")

        _httpx.AsyncClient.post = _timeout
        try:
            payment_header = build_payment_header()
            resp = gateway.post(
                "/v1/chat/completions",
                headers={"X-Payment": payment_header},
            )
            assert "Facilitator unreachable" in resp.json()["error"]
        finally:
            _httpx.AsyncClient.post = original_post

    def test_facilitator_5xx_returns_402(self, gateway, facilitator_config):
        import httpx as _httpx

        original_post = _httpx.AsyncClient.post

        async def _server_error(self, url, **kwargs):
            return _httpx.Response(500, text="Internal Server Error")

        _httpx.AsyncClient.post = _server_error
        try:
            payment_header = build_payment_header()
            resp = gateway.post(
                "/v1/chat/completions",
                headers={"X-Payment": payment_header},
            )
            assert resp.status_code == 402
            assert "500" in resp.json()["error"]
        finally:
            _httpx.AsyncClient.post = original_post


class TestDuplicatePayment:
    """The same signed payment cannot be used twice."""

    def test_duplicate_payment_rejected_on_second_use(self, gateway, facilitator_config):
        facilitator_config.reject_duplicates = True
        sig = "0xunique_signature_abc"
        payment_header = build_payment_header(signature=sig)

        # First use — succeeds
        resp1 = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert resp1.status_code == 200

        # Second use with the same signature — rejected
        resp2 = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": payment_header},
        )
        assert resp2.status_code == 402
        assert "Duplicate" in resp2.json()["error"]

    def test_different_signatures_each_succeed(self, gateway, facilitator_config):
        facilitator_config.reject_duplicates = True

        resp1 = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": build_payment_header(signature="0xsig_one")},
        )
        assert resp1.status_code == 200

        resp2 = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": build_payment_header(signature="0xsig_two")},
        )
        assert resp2.status_code == 200


class TestMalformedPaymentHeader:
    """Client sends a syntactically invalid X-Payment header."""

    def test_malformed_header_returns_400(self, gateway):
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": "!!!not-valid-base64-or-json!!!"},
        )
        assert resp.status_code == 400

    def test_malformed_header_error_message(self, gateway):
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": "!!!"},
        )
        assert "Malformed" in resp.json().get("error", "")

    def test_empty_payment_header_returns_402(self, gateway):
        # Missing header entirely → still asks for payment
        resp = gateway.post("/v1/chat/completions")
        assert resp.status_code == 402

    def test_base64_encoded_payment_accepted(self, gateway):
        """Both raw JSON and base64 encodings of the X-Payment header are valid."""
        import json as _json

        raw_payload = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "eip155:8453",
            "payload": {
                "signature": "0xb64test",
                "authorization": {
                    "from": PAYER_ADDRESS,
                    "to": PAYMENT_ADDRESS,
                    "value": "10000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0xb64nonce",
                },
            },
        }
        b64_header = base64.b64encode(_json.dumps(raw_payload).encode()).decode()
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": b64_header},
        )
        assert resp.status_code == 200

    def test_raw_json_payment_accepted(self, gateway):
        import json as _json

        raw_payload = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "eip155:8453",
            "payload": {
                "signature": "0xjsontest",
                "authorization": {
                    "from": PAYER_ADDRESS,
                    "to": PAYMENT_ADDRESS,
                    "value": "10000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0xjsonnonce",
                },
            },
        }
        resp = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": _json.dumps(raw_payload)},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 4. Multi-request sequence — validates state isolation between requests
# ---------------------------------------------------------------------------


class TestMultiRequestIsolation:
    """Each request is stateless; prior payment outcomes do not bleed between calls."""

    def test_valid_payment_after_invalid_payment_succeeds(self, gateway, facilitator_config):
        # First request: payment rejected
        facilitator_config.is_valid = False
        facilitator_config.invalid_reason = "Signature invalid"
        resp_bad = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": build_payment_header(signature="0xbad")},
        )
        assert resp_bad.status_code == 402

        # Second request: payment accepted
        facilitator_config.is_valid = True
        facilitator_config.invalid_reason = None
        resp_ok = gateway.post(
            "/v1/chat/completions",
            headers={"X-Payment": build_payment_header(signature="0xgood")},
        )
        assert resp_ok.status_code == 200

    def test_multiple_sequential_payments_all_succeed(self, gateway):
        for i in range(5):
            resp = gateway.post(
                "/v1/chat/completions",
                headers={"X-Payment": build_payment_header(signature=f"0xsig_{i}")},
            )
            assert resp.status_code == 200, f"Request {i} failed: {resp.json()}"

    def test_facilitator_called_once_per_payment_request(self, gateway, facilitator_config):
        for i in range(3):
            gateway.post(
                "/v1/chat/completions",
                headers={"X-Payment": build_payment_header(signature=f"0xsig_{i}")},
            )
        # Each paid request calls the facilitator exactly once
        assert len(facilitator_config.received_requests) == 3
