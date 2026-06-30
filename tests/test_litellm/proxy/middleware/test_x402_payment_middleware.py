"""
Unit tests for X402PaymentMiddleware and supporting helpers.

All tests are pure-unit: no network calls, no database.
Facilitator calls are monkeypatched; ASGI is exercised via starlette's TestClient.
"""

import base64
import json
import os
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from litellm.proxy.middleware.x402_payment_middleware import (
    X402PaymentMiddleware,
    _b58decode,
    _build_chain_configs,
    _facilitator_url_for,
    verify_payment_local,
)
from litellm.types.x402 import (
    NETWORK_ARBITRUM,
    NETWORK_BASE,
    NETWORK_SOLANA,
    X402PaymentAccept,
    X402PaymentPayload,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(env: Dict[str, str] | None = None, **env_overrides):
    """Build a minimal Starlette app wrapped in X402PaymentMiddleware."""
    env = env or {}
    env.update(env_overrides)

    async def echo(request: Request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/v1/chat/completions", echo, methods=["POST"])])
    app.add_middleware(X402PaymentMiddleware)
    return app, env


def _encode_payment(payload: dict) -> bytes:
    return base64.b64encode(json.dumps(payload).encode("utf-8"))


def _evm_payment(network: str = NETWORK_BASE) -> dict:
    return {
        "x402Version": 1,
        "scheme": "exact",
        "network": network,
        "payload": {
            "signature": "0x" + "ab" * 65,
            "authorization": {
                "from": "0xDeAdBeEf" + "0" * 32,
                "to": "0x" + "aa" * 20,
                "value": "1000000",
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + "cc" * 32,
            },
        },
    }


def _chain_config_for(
    network: str, pay_to: str = "0x" + "aa" * 20
) -> X402PaymentAccept:
    from litellm.types.x402 import (
        USDC_CONTRACT_BASE,
        USDC_CONTRACT_ARBITRUM,
        USDC_MINT_SOLANA,
    )

    assets = {
        NETWORK_BASE: USDC_CONTRACT_BASE,
        NETWORK_ARBITRUM: USDC_CONTRACT_ARBITRUM,
        NETWORK_SOLANA: USDC_MINT_SOLANA,
    }
    return X402PaymentAccept(
        network=network,
        maxAmountRequired="1000000",
        resource="https://api.xcity.ai",
        payTo=pay_to,
        asset=assets[network],
    )


# ---------------------------------------------------------------------------
# base58 helper
# ---------------------------------------------------------------------------


class TestBase58Decode:
    def test_known_value(self):
        # base58check of the zero address is "1" * 34 for 20 zero bytes
        # Just verify round-trip for a known Solana-like key
        # Solana's all-zero pubkey in base58 is "11111111111111111111111111111111"
        result = _b58decode("11111111111111111111111111111111")
        assert result == bytes(32)

    def test_leading_ones(self):
        # A single "1" decodes to a single zero byte
        assert _b58decode("1") == bytes(1)


# ---------------------------------------------------------------------------
# _build_chain_configs
# ---------------------------------------------------------------------------


class TestBuildChainConfigs:
    def test_empty_when_no_env(self, monkeypatch):
        for var in (
            "PAYMENT_ADDRESS_BASE",
            "PAYMENT_ADDRESS_ARBITRUM",
            "PAYMENT_ADDRESS_SOLANA",
        ):
            monkeypatch.delenv(var, raising=False)
        configs = _build_chain_configs("https://api.xcity.ai", "1000000")
        assert configs == []

    def test_base_only(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_ADDRESS_BASE", "0x" + "aa" * 20)
        monkeypatch.delenv("PAYMENT_ADDRESS_ARBITRUM", raising=False)
        monkeypatch.delenv("PAYMENT_ADDRESS_SOLANA", raising=False)
        configs = _build_chain_configs("https://api.xcity.ai", "1000000")
        assert len(configs) == 1
        assert configs[0].network == NETWORK_BASE

    def test_all_three_chains(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_ADDRESS_BASE", "0x" + "aa" * 20)
        monkeypatch.setenv("PAYMENT_ADDRESS_ARBITRUM", "0x" + "bb" * 20)
        monkeypatch.setenv("PAYMENT_ADDRESS_SOLANA", "SomeSolanaAddress")
        configs = _build_chain_configs("https://api.xcity.ai", "1000000")
        networks = [c.network for c in configs]
        assert NETWORK_BASE in networks
        assert NETWORK_ARBITRUM in networks
        assert NETWORK_SOLANA in networks

    def test_usdc_amount_propagated(self, monkeypatch):
        monkeypatch.setenv("PAYMENT_ADDRESS_BASE", "0x" + "aa" * 20)
        monkeypatch.delenv("PAYMENT_ADDRESS_ARBITRUM", raising=False)
        monkeypatch.delenv("PAYMENT_ADDRESS_SOLANA", raising=False)
        configs = _build_chain_configs("https://api.xcity.ai", "500000")
        assert configs[0].maxAmountRequired == "500000"


# ---------------------------------------------------------------------------
# _facilitator_url_for
# ---------------------------------------------------------------------------


class TestFacilitatorUrlFor:
    def test_base(self, monkeypatch):
        monkeypatch.setenv("FACILITATOR_BASE_URL", "https://fac.base.example.com")
        assert _facilitator_url_for(NETWORK_BASE) == "https://fac.base.example.com"

    def test_arbitrum(self, monkeypatch):
        monkeypatch.setenv("FACILITATOR_ARBITRUM_URL", "https://fac.arb.example.com")
        assert _facilitator_url_for(NETWORK_ARBITRUM) == "https://fac.arb.example.com"

    def test_solana(self, monkeypatch):
        monkeypatch.setenv("FACILITATOR_SOLANA_URL", "https://fac.sol.example.com")
        assert _facilitator_url_for(NETWORK_SOLANA) == "https://fac.sol.example.com"

    def test_unknown_network(self):
        assert _facilitator_url_for("cosmos:cosmoshub-4") is None

    def test_missing_env(self, monkeypatch):
        monkeypatch.delenv("FACILITATOR_BASE_URL", raising=False)
        assert _facilitator_url_for(NETWORK_BASE) is None


# ---------------------------------------------------------------------------
# verify_payment_local — EVM
# ---------------------------------------------------------------------------


class TestVerifyEVM:
    def test_valid_evm_base(self):
        pay_to = "0x" + "aa" * 20
        payment = X402PaymentPayload(
            x402Version=1,
            scheme="exact",
            network=NETWORK_BASE,
            payload={
                "signature": "0x" + "ab" * 65,
                "authorization": {
                    "from": "0xDeAdBeEf" + "0" * 32,
                    "to": pay_to,
                    "value": "1000000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0x" + "cc" * 32,
                },
            },
        )
        cfg = _chain_config_for(NETWORK_BASE, pay_to)
        verify_payment_local(payment, cfg)  # must not raise

    def test_destination_mismatch_raises(self):
        payment = X402PaymentPayload(
            x402Version=1,
            scheme="exact",
            network=NETWORK_BASE,
            payload={
                "signature": "0x" + "ab" * 65,
                "authorization": {
                    "from": "0xDeAdBeEf" + "0" * 32,
                    "to": "0x" + "bb" * 20,  # wrong destination
                    "value": "1000000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0x" + "cc" * 32,
                },
            },
        )
        cfg = _chain_config_for(NETWORK_BASE, "0x" + "aa" * 20)
        with pytest.raises(ValueError, match="destination"):
            verify_payment_local(payment, cfg)

    def test_malformed_signature_raises(self):
        pay_to = "0x" + "aa" * 20
        payment = X402PaymentPayload(
            x402Version=1,
            scheme="exact",
            network=NETWORK_BASE,
            payload={
                "signature": "notasig",
                "authorization": {"from": "0xfoo", "to": pay_to, "value": "1"},
            },
        )
        cfg = _chain_config_for(NETWORK_BASE, pay_to)
        with pytest.raises(ValueError):
            verify_payment_local(payment, cfg)

    def test_arbitrum_network_accepted(self):
        pay_to = "0x" + "cc" * 20
        payment = X402PaymentPayload(
            x402Version=1,
            scheme="exact",
            network=NETWORK_ARBITRUM,
            payload={
                "signature": "0x" + "ab" * 65,
                "authorization": {
                    "from": "0xDeAdBeEf" + "0" * 32,
                    "to": pay_to,
                    "value": "1000000",
                    "validAfter": "0",
                    "validBefore": "9999999999",
                    "nonce": "0x" + "cc" * 32,
                },
            },
        )
        cfg = _chain_config_for(NETWORK_ARBITRUM, pay_to)
        verify_payment_local(payment, cfg)  # must not raise


# ---------------------------------------------------------------------------
# verify_payment_local — Solana (Ed25519 via cryptography lib)
# ---------------------------------------------------------------------------


class TestVerifySolana:
    """Generate a real Ed25519 keypair and sign a canonical message to test the verifier."""

    @pytest.fixture(autouse=True)
    def _gen_keypair(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        self._private_key = Ed25519PrivateKey.generate()
        self._public_key = self._private_key.public_key()
        self._pubkey_bytes = self._public_key.public_bytes_raw()

    def _pubkey_b58(self) -> str:
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        num = int.from_bytes(self._pubkey_bytes, "big")
        chars = []
        while num:
            chars.append(alphabet[num % 58])
            num //= 58
        return "".join(reversed(chars))

    def _sign_auth(self, auth: dict) -> str:
        message = json.dumps(auth, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        sig_bytes = self._private_key.sign(message)
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        num = int.from_bytes(sig_bytes, "big")
        chars = []
        while num:
            chars.append(alphabet[num % 58])
            num //= 58
        return "".join(reversed(chars))

    def test_valid_ed25519(self):
        pay_to = "TokenAccountBASE58here123"
        pubkey = self._pubkey_b58()
        auth = {"from": pubkey, "to": pay_to, "value": "1000000", "nonce": "abc123"}
        sig = self._sign_auth(auth)

        payment = X402PaymentPayload(
            x402Version=1,
            scheme="exact",
            network=NETWORK_SOLANA,
            payload={"signature": sig, "authorization": auth},
        )
        cfg = _chain_config_for(NETWORK_SOLANA, pay_to)
        verify_payment_local(payment, cfg)  # must not raise

    def test_wrong_signature_raises(self):
        pay_to = "TokenAccountBASE58here123"
        pubkey = self._pubkey_b58()
        # Use a different private key to sign
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        other_key = Ed25519PrivateKey.generate()
        auth = {"from": pubkey, "to": pay_to, "value": "1000000", "nonce": "abc123"}
        message = json.dumps(auth, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        bad_sig_bytes = other_key.sign(message)
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        num = int.from_bytes(bad_sig_bytes, "big")
        chars = []
        while num:
            chars.append(alphabet[num % 58])
            num //= 58
        bad_sig = "".join(reversed(chars))

        payment = X402PaymentPayload(
            x402Version=1,
            scheme="exact",
            network=NETWORK_SOLANA,
            payload={"signature": bad_sig, "authorization": auth},
        )
        cfg = _chain_config_for(NETWORK_SOLANA, pay_to)
        with pytest.raises(ValueError, match="Ed25519"):
            verify_payment_local(payment, cfg)

    def test_destination_mismatch_raises(self):
        pubkey = self._pubkey_b58()
        auth = {
            "from": pubkey,
            "to": "WrongRecipient",
            "value": "1000000",
            "nonce": "nnn",
        }
        sig = self._sign_auth(auth)

        payment = X402PaymentPayload(
            x402Version=1,
            scheme="exact",
            network=NETWORK_SOLANA,
            payload={"signature": sig, "authorization": auth},
        )
        cfg = _chain_config_for(NETWORK_SOLANA, "CorrectRecipient")
        with pytest.raises(ValueError, match="recipient"):
            verify_payment_local(payment, cfg)


# ---------------------------------------------------------------------------
# ASGI middleware integration
# ---------------------------------------------------------------------------


class TestX402MiddlewareASGI:
    """Full ASGI round-trips using Starlette TestClient."""

    def _client(self, monkeypatch, extra_env=None):
        defaults = {
            "X402_ENABLED": "true",
            "PAYMENT_ADDRESS_BASE": "0x" + "aa" * 20,
            "X402_RESOURCE_URL": "https://api.xcity.ai",
            "X402_AMOUNT_USDC": "1000000",
        }
        if extra_env:
            defaults.update(extra_env)
        for k, v in defaults.items():
            monkeypatch.setenv(k, v)
        # Clear facilitator URLs so local verification is used
        for var in (
            "FACILITATOR_BASE_URL",
            "FACILITATOR_ARBITRUM_URL",
            "FACILITATOR_SOLANA_URL",
        ):
            monkeypatch.delenv(var, raising=False)

        async def echo(request: Request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/v1/chat/completions", echo, methods=["POST"])])
        app.add_middleware(X402PaymentMiddleware)
        return TestClient(app, raise_server_exceptions=True)

    # ------------------------------------------------------------------
    # Disabled gate
    # ------------------------------------------------------------------

    def test_disabled_passes_through(self, monkeypatch):
        monkeypatch.setenv("X402_ENABLED", "false")
        monkeypatch.setenv("PAYMENT_ADDRESS_BASE", "0x" + "aa" * 20)

        async def echo(request: Request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/v1/chat/completions", echo, methods=["POST"])])
        app.add_middleware(X402PaymentMiddleware)
        client = TestClient(app)
        resp = client.post("/v1/chat/completions")
        assert resp.status_code == 200

    # ------------------------------------------------------------------
    # No X-PAYMENT → 402
    # ------------------------------------------------------------------

    def test_missing_payment_returns_402(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.post("/v1/chat/completions")
        assert resp.status_code == 402
        body = resp.json()
        assert body["x402Version"] == 1
        assert "accepts" in body
        assert len(body["accepts"]) >= 1
        assert body["accepts"][0]["network"] == NETWORK_BASE

    def test_402_includes_all_configured_chains(self, monkeypatch):
        client = self._client(
            monkeypatch,
            extra_env={
                "PAYMENT_ADDRESS_ARBITRUM": "0x" + "bb" * 20,
                "PAYMENT_ADDRESS_SOLANA": "SolAddrXXXX",
            },
        )
        resp = client.post("/v1/chat/completions")
        assert resp.status_code == 402
        networks = [a["network"] for a in resp.json()["accepts"]]
        assert NETWORK_BASE in networks
        assert NETWORK_ARBITRUM in networks
        assert NETWORK_SOLANA in networks

    def test_non_gated_path_passes_through(self, monkeypatch):
        monkeypatch.setenv("X402_ENABLED", "true")
        monkeypatch.setenv("PAYMENT_ADDRESS_BASE", "0x" + "aa" * 20)

        async def health(request: Request):
            return JSONResponse({"status": "ok"})

        app = Starlette(routes=[Route("/health", health, methods=["GET"])])
        app.add_middleware(X402PaymentMiddleware)
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200

    # ------------------------------------------------------------------
    # Accept header → preferred network ordering
    # ------------------------------------------------------------------

    def test_accept_header_reorders_accepts(self, monkeypatch):
        client = self._client(
            monkeypatch,
            extra_env={"PAYMENT_ADDRESS_ARBITRUM": "0x" + "bb" * 20},
        )
        resp = client.post(
            "/v1/chat/completions",
            headers={"Accept": f"application/x402+json; network={NETWORK_ARBITRUM}"},
        )
        assert resp.status_code == 402
        # Arbitrum should be first
        assert resp.json()["accepts"][0]["network"] == NETWORK_ARBITRUM

    # ------------------------------------------------------------------
    # Malformed X-PAYMENT → 402
    # ------------------------------------------------------------------

    def test_malformed_payment_header(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.post(
            "/v1/chat/completions",
            headers={"X-PAYMENT": "not-valid-base64!!!!"},
        )
        assert resp.status_code == 402
        assert "Malformed" in resp.json()["error"]

    def test_unsupported_network(self, monkeypatch):
        client = self._client(monkeypatch)
        payload = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "cosmos:cosmoshub-4",
            "payload": {"signature": "0x" + "ab" * 65, "authorization": {}},
        }
        resp = client.post(
            "/v1/chat/completions",
            headers={"X-PAYMENT": _encode_payment(payload)},
        )
        assert resp.status_code == 402
        assert "not supported" in resp.json()["error"]

    # ------------------------------------------------------------------
    # Valid EVM payment (local verification, no facilitator)
    # ------------------------------------------------------------------

    def test_valid_evm_payment_passes(self, monkeypatch):
        pay_to = "0x" + "aa" * 20
        client = self._client(monkeypatch, extra_env={"PAYMENT_ADDRESS_BASE": pay_to})

        payload = _evm_payment(NETWORK_BASE)
        payload["payload"]["authorization"]["to"] = pay_to

        resp = client.post(
            "/v1/chat/completions",
            headers={"X-PAYMENT": _encode_payment(payload)},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_valid_evm_payment_adds_response_header(self, monkeypatch):
        pay_to = "0x" + "aa" * 20
        client = self._client(monkeypatch, extra_env={"PAYMENT_ADDRESS_BASE": pay_to})
        payload = _evm_payment(NETWORK_BASE)
        payload["payload"]["authorization"]["to"] = pay_to

        resp = client.post(
            "/v1/chat/completions",
            headers={"X-PAYMENT": _encode_payment(payload)},
        )
        assert resp.status_code == 200
        assert "x-payment-response" in resp.headers
        receipt = json.loads(resp.headers["x-payment-response"])
        assert receipt["success"] is True
        assert receipt["network"] == NETWORK_BASE

    def test_evm_destination_mismatch_rejected(self, monkeypatch):
        pay_to = "0x" + "aa" * 20
        client = self._client(monkeypatch, extra_env={"PAYMENT_ADDRESS_BASE": pay_to})
        payload = _evm_payment(NETWORK_BASE)
        # Wrong destination
        payload["payload"]["authorization"]["to"] = "0x" + "ff" * 20

        resp = client.post(
            "/v1/chat/completions",
            headers={"X-PAYMENT": _encode_payment(payload)},
        )
        assert resp.status_code == 402

    # ------------------------------------------------------------------
    # Facilitator-delegated verification
    # ------------------------------------------------------------------

    def test_facilitator_verify_valid(self, monkeypatch):
        pay_to = "0x" + "aa" * 20
        monkeypatch.setenv("X402_ENABLED", "true")
        monkeypatch.setenv("PAYMENT_ADDRESS_BASE", pay_to)
        monkeypatch.setenv("FACILITATOR_BASE_URL", "https://facilitator.example.com")
        monkeypatch.delenv("PAYMENT_ADDRESS_ARBITRUM", raising=False)
        monkeypatch.delenv("PAYMENT_ADDRESS_SOLANA", raising=False)
        monkeypatch.delenv("FACILITATOR_ARBITRUM_URL", raising=False)
        monkeypatch.delenv("FACILITATOR_SOLANA_URL", raising=False)

        async def _mock_verify(url, payload, requirements):
            return True, None

        with patch(
            "litellm.proxy.middleware.x402_payment_middleware.facilitator_verify",
            new=_mock_verify,
        ):
            payload = _evm_payment(NETWORK_BASE)
            payload["payload"]["authorization"]["to"] = pay_to

            async def echo(request: Request):
                return JSONResponse({"ok": True})

            app = Starlette(
                routes=[Route("/v1/chat/completions", echo, methods=["POST"])]
            )
            app.add_middleware(X402PaymentMiddleware)
            client = TestClient(app)
            resp = client.post(
                "/v1/chat/completions",
                headers={"X-PAYMENT": _encode_payment(payload)},
            )
        assert resp.status_code == 200

    def test_facilitator_verify_invalid(self, monkeypatch):
        pay_to = "0x" + "aa" * 20
        monkeypatch.setenv("X402_ENABLED", "true")
        monkeypatch.setenv("PAYMENT_ADDRESS_BASE", pay_to)
        monkeypatch.setenv("FACILITATOR_BASE_URL", "https://facilitator.example.com")
        monkeypatch.delenv("PAYMENT_ADDRESS_ARBITRUM", raising=False)
        monkeypatch.delenv("PAYMENT_ADDRESS_SOLANA", raising=False)
        monkeypatch.delenv("FACILITATOR_ARBITRUM_URL", raising=False)
        monkeypatch.delenv("FACILITATOR_SOLANA_URL", raising=False)

        async def _mock_verify(url, payload, requirements):
            return False, "Insufficient balance"

        with patch(
            "litellm.proxy.middleware.x402_payment_middleware.facilitator_verify",
            new=_mock_verify,
        ):
            payload = _evm_payment(NETWORK_BASE)
            payload["payload"]["authorization"]["to"] = pay_to

            async def echo(request: Request):
                return JSONResponse({"ok": True})

            app = Starlette(
                routes=[Route("/v1/chat/completions", echo, methods=["POST"])]
            )
            app.add_middleware(X402PaymentMiddleware)
            client = TestClient(app)
            resp = client.post(
                "/v1/chat/completions",
                headers={"X-PAYMENT": _encode_payment(payload)},
            )
        assert resp.status_code == 402
        assert "Insufficient balance" in resp.json()["error"]

    # ------------------------------------------------------------------
    # X402_PROXY_API_KEY injection
    # ------------------------------------------------------------------

    def test_proxy_api_key_injected(self, monkeypatch):
        pay_to = "0x" + "aa" * 20
        monkeypatch.setenv("X402_ENABLED", "true")
        monkeypatch.setenv("PAYMENT_ADDRESS_BASE", pay_to)
        monkeypatch.setenv("X402_PROXY_API_KEY", "sk-internal-test-key")
        monkeypatch.delenv("PAYMENT_ADDRESS_ARBITRUM", raising=False)
        monkeypatch.delenv("PAYMENT_ADDRESS_SOLANA", raising=False)
        monkeypatch.delenv("FACILITATOR_BASE_URL", raising=False)

        captured_auth = {}

        async def echo(request: Request):
            captured_auth["value"] = request.headers.get("authorization", "")
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/v1/chat/completions", echo, methods=["POST"])])
        app.add_middleware(X402PaymentMiddleware)
        client = TestClient(app)

        payload = _evm_payment(NETWORK_BASE)
        payload["payload"]["authorization"]["to"] = pay_to
        client.post(
            "/v1/chat/completions",
            headers={"X-PAYMENT": _encode_payment(payload)},
        )
        assert captured_auth.get("value") == "Bearer sk-internal-test-key"

    # ------------------------------------------------------------------
    # No PAYMENT_ADDRESS_* configured → pass through
    # ------------------------------------------------------------------

    def test_no_addresses_passes_through(self, monkeypatch):
        monkeypatch.setenv("X402_ENABLED", "true")
        monkeypatch.delenv("PAYMENT_ADDRESS_BASE", raising=False)
        monkeypatch.delenv("PAYMENT_ADDRESS_ARBITRUM", raising=False)
        monkeypatch.delenv("PAYMENT_ADDRESS_SOLANA", raising=False)

        async def echo(request: Request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/v1/chat/completions", echo, methods=["POST"])])
        app.add_middleware(X402PaymentMiddleware)
        client = TestClient(app)
        resp = client.post("/v1/chat/completions")
        assert resp.status_code == 200

    # ------------------------------------------------------------------
    # ASGI non-HTTP scopes pass through
    # ------------------------------------------------------------------

    def test_is_not_base_http_middleware(self):
        """Must be a pure ASGI class, not BaseHTTPMiddleware."""
        from starlette.middleware.base import BaseHTTPMiddleware

        assert not issubclass(X402PaymentMiddleware, BaseHTTPMiddleware)

    def test_has_asgi_call_protocol(self):
        assert "__call__" in X402PaymentMiddleware.__dict__
