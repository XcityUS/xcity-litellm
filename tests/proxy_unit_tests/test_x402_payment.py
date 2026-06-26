"""
Unit tests for the x402 payment middleware and CDP facilitator client.
"""

import asyncio
import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from litellm.proxy.x402.facilitator import (
    CDPFacilitatorClient,
    SettleResult,
    VerifyResult,
    build_facilitator,
)
from litellm.proxy.x402.middleware import (
    X402Config,
    X402PaymentMiddleware,
    _DEFAULT_PROTECTED_ROUTES,
)

# ---------------------------------------------------------------------------
# X402Config tests
# ---------------------------------------------------------------------------


class TestX402Config:
    def test_defaults(self):
        cfg = X402Config()
        assert cfg.enabled is False
        assert cfg.facilitator == "coinbase_cdp"
        assert cfg.network == "base"
        assert cfg.max_timeout_seconds == 300
        assert cfg.payment_header == "X-PAYMENT"
        assert cfg.protected_routes == []

    def test_from_dict_minimal(self):
        cfg = X402Config.from_dict({"enabled": True, "pay_to": "0xABCD"})
        assert cfg.enabled is True
        assert cfg.pay_to == "0xABCD"
        assert cfg.facilitator == "coinbase_cdp"

    def test_from_dict_full(self):
        raw = {
            "enabled": True,
            "facilitator": "coinbase_cdp",
            "facilitator_url": "https://example.com",
            "api_key": "test-key",
            "pay_to": "0x1234",
            "amount": "500000",
            "asset": "0xasset",
            "network": "base-sepolia",
            "description": "Pay me",
            "max_timeout_seconds": 60,
            "payment_header": "PAYMENT-SIGNATURE",
            "protected_routes": ["/v1/chat/completions"],
        }
        cfg = X402Config.from_dict(raw)
        assert cfg.enabled is True
        assert cfg.facilitator_url == "https://example.com"
        assert cfg.api_key == "test-key"
        assert cfg.amount == "500000"
        assert cfg.network == "base-sepolia"
        assert cfg.payment_header == "PAYMENT-SIGNATURE"
        assert cfg.protected_routes == ["/v1/chat/completions"]

    def test_payment_requirements_shape(self):
        cfg = X402Config(pay_to="0xWALLET", amount="1000000", network="base")
        req = cfg.payment_requirements("https://api.example.com/v1/chat/completions")
        assert req["scheme"] == "exact"
        assert req["network"] == "base"
        assert req["maxAmountRequired"] == "1000000"
        assert req["payTo"] == "0xWALLET"
        assert req["resource"] == "https://api.example.com/v1/chat/completions"

    def test_x402_body_is_valid_json(self):
        cfg = X402Config(pay_to="0xWALLET", amount="1000000")
        body = cfg.x402_body("https://example.com/v1/chat")
        parsed = json.loads(body)
        assert parsed["x402Version"] == 1
        assert "accepts" in parsed
        assert len(parsed["accepts"]) == 1


# ---------------------------------------------------------------------------
# CDPFacilitatorClient tests
# ---------------------------------------------------------------------------


class TestCDPFacilitatorClient:
    def _make_client(self, base_url: Optional[str] = None) -> CDPFacilitatorClient:
        return CDPFacilitatorClient(
            base_url=base_url or "https://api.example.com",
            api_key="test-key",
        )

    @pytest.mark.asyncio
    async def test_verify_success(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "isValid": True,
            "payer": "0xPAYER",
            "invalidReason": None,
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await client.verify("pay-token", {"scheme": "exact"})

        assert result.is_valid is True
        assert result.payer == "0xPAYER"
        assert result.invalid_reason is None

    @pytest.mark.asyncio
    async def test_verify_invalid_payment(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "isValid": False,
            "payer": None,
            "invalidReason": "insufficient funds",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await client.verify("bad-token", {})

        assert result.is_valid is False
        assert result.invalid_reason == "insufficient funds"

    @pytest.mark.asyncio
    async def test_settle_success(self):
        client = self._make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "txHash": "0xDEADBEEF",
            "networkId": "base",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await client.settle("pay-token", {"scheme": "exact"})

        assert result.success is True
        assert result.tx_hash == "0xDEADBEEF"
        assert result.network_id == "base"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_settle_network_error_returns_failed_result(self):
        client = self._make_client()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.post = AsyncMock(side_effect=Exception("timeout"))
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await client.settle("pay-token", {})

        assert result.success is False
        assert "timeout" in (result.error or "")

    @pytest.mark.asyncio
    async def test_settle_with_retry_succeeds_on_second_attempt(self):
        client = self._make_client()

        call_count = 0

        async def _fake_settle(payment, requirements):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return SettleResult(
                    success=False,
                    tx_hash=None,
                    network_id=None,
                    error="temporary error",
                )
            return SettleResult(success=True, tx_hash="0xOK", network_id="base")

        client.settle = _fake_settle  # type: ignore[method-assign]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client.settle_with_retry("pay-token", {})

        assert result.success is True
        assert result.tx_hash == "0xOK"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_settle_with_retry_exhausts_all_attempts(self):
        client = self._make_client()
        call_count = 0

        async def _always_fail(payment, requirements):
            nonlocal call_count
            call_count += 1
            return SettleResult(
                success=False, tx_hash=None, network_id=None, error="permanent"
            )

        client.settle = _always_fail  # type: ignore[method-assign]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client.settle_with_retry("pay-token", {})

        assert result.success is False
        assert call_count == 3  # MAX_SETTLE_RETRIES

    def test_headers_include_api_key(self):
        client = CDPFacilitatorClient(api_key="my-key")
        headers = client._headers()
        assert headers["X-API-Key"] == "my-key"
        assert headers["Content-Type"] == "application/json"

    def test_headers_no_api_key(self):
        client = CDPFacilitatorClient(api_key=None)
        headers = client._headers()
        assert "X-API-Key" not in headers


# ---------------------------------------------------------------------------
# build_facilitator tests
# ---------------------------------------------------------------------------


class TestBuildFacilitator:
    def test_builds_cdp_client(self):
        client = build_facilitator("coinbase_cdp", None, None)
        assert isinstance(client, CDPFacilitatorClient)

    def test_aliases_accepted(self):
        for alias in ("cdp", "coinbase"):
            client = build_facilitator(alias, None, None)
            assert isinstance(client, CDPFacilitatorClient)

    def test_unknown_facilitator_raises(self):
        with pytest.raises(ValueError, match="Unknown x402 facilitator"):
            build_facilitator("stripe_x402", None, None)


# ---------------------------------------------------------------------------
# X402PaymentMiddleware tests
# ---------------------------------------------------------------------------


def _make_scope(path: str, headers: Optional[List] = None) -> dict:
    return {
        "type": "http",
        "path": path,
        "method": "POST",
        "headers": headers or [],
        "scheme": "https",
        "server": ("api.example.com", 443),
        "query_string": b"",
    }


def _make_config(**kwargs) -> X402Config:
    defaults = dict(
        enabled=True,
        pay_to="0xWALLET",
        amount="1000000",
        network="base",
    )
    defaults.update(kwargs)
    return X402Config(**defaults)


class TestX402PaymentMiddleware:
    def _make_middleware(
        self, config: X402Config, inner_app=None
    ) -> X402PaymentMiddleware:
        async def _noop_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        return X402PaymentMiddleware(
            app=inner_app or _noop_app,
            config=config,
        )

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self):
        mw = self._make_middleware(_make_config())
        scope = {"type": "websocket"}
        receive = AsyncMock()
        send = AsyncMock()

        inner_called = []

        async def inner(s, r, snd):
            inner_called.append(True)
            await snd({"type": "websocket.connect"})

        mw.app = inner
        await mw(scope, receive, send)
        assert inner_called

    @pytest.mark.asyncio
    async def test_unprotected_route_passes_through(self):
        cfg = _make_config(protected_routes=["/v1/chat/completions"])
        mw = self._make_middleware(cfg)

        inner_called = []

        async def inner(scope, receive, send):
            inner_called.append(True)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw.app = inner
        sent = []

        async def mock_send(msg):
            sent.append(msg)

        await mw(_make_scope("/health"), AsyncMock(), mock_send)
        assert inner_called

    @pytest.mark.asyncio
    async def test_missing_payment_header_returns_402(self):
        cfg = _make_config(protected_routes=["/v1/chat/completions"])
        mw = self._make_middleware(cfg)

        sent: List[dict] = []

        async def mock_send(msg):
            sent.append(msg)

        await mw(
            _make_scope("/v1/chat/completions"),
            AsyncMock(),
            mock_send,
        )
        assert sent[0]["status"] == 402
        body = json.loads(sent[1]["body"])
        assert body["x402Version"] == 1
        assert "accepts" in body

    @pytest.mark.asyncio
    async def test_valid_payment_passes_through_and_settles(self):
        cfg = _make_config(protected_routes=["/v1/chat/completions"])

        async def fake_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b'{"ok": true}'})

        # Inject mock facilitator via facilitator= to avoid hitting real CDP
        mock_facilitator = AsyncMock()
        mock_facilitator.verify = AsyncMock(
            return_value=VerifyResult(
                is_valid=True, payer="0xPAYER", invalid_reason=None
            )
        )
        settle_called = []

        async def _fake_settle_with_retry(payment, requirements):
            settle_called.append((payment, requirements))
            return SettleResult(success=True, tx_hash="0xTXHASH", network_id="base")

        mock_facilitator.settle_with_retry = _fake_settle_with_retry
        mw = X402PaymentMiddleware(
            app=fake_app, config=cfg, facilitator=mock_facilitator
        )

        sent: List[dict] = []

        async def mock_send(msg):
            sent.append(msg)

        scope = _make_scope(
            "/v1/chat/completions",
            headers=[(b"x-payment", b"valid-payment-token")],
        )
        await mw(scope, AsyncMock(), mock_send)

        # App response should be passed through
        assert sent[0]["status"] == 200
        # Allow the background task to run
        await asyncio.sleep(0)
        assert len(settle_called) == 1
        assert settle_called[0][0] == "valid-payment-token"

    @pytest.mark.asyncio
    async def test_invalid_payment_returns_402(self):
        cfg = _make_config(protected_routes=["/v1/chat/completions"])

        mock_facilitator = AsyncMock()
        mock_facilitator.verify = AsyncMock(
            return_value=VerifyResult(
                is_valid=False, payer=None, invalid_reason="bad signature"
            )
        )
        mw = self._make_middleware(cfg)
        mw._facilitator = mock_facilitator
        mw._facilitator_key = "__injected__"

        sent: List[dict] = []

        async def mock_send(msg):
            sent.append(msg)

        scope = _make_scope(
            "/v1/chat/completions",
            headers=[(b"x-payment", b"bad-payment-token")],
        )
        await mw(scope, AsyncMock(), mock_send)

        assert sent[0]["status"] == 402
        body = json.loads(sent[1]["body"])
        assert "bad signature" in body.get("error", "")

    @pytest.mark.asyncio
    async def test_verify_exception_returns_402(self):
        cfg = _make_config(protected_routes=["/v1/chat/completions"])

        mock_facilitator = AsyncMock()
        mock_facilitator.verify = AsyncMock(side_effect=Exception("network error"))
        mw = self._make_middleware(cfg)
        mw._facilitator = mock_facilitator
        mw._facilitator_key = "__injected__"

        sent: List[dict] = []

        async def mock_send(msg):
            sent.append(msg)

        scope = _make_scope(
            "/v1/chat/completions",
            headers=[(b"x-payment", b"some-token")],
        )
        await mw(scope, AsyncMock(), mock_send)
        assert sent[0]["status"] == 402

    @pytest.mark.asyncio
    async def test_payment_signature_alias_header_accepted(self):
        cfg = _make_config(protected_routes=["/v1/chat/completions"])

        mock_facilitator = AsyncMock()
        mock_facilitator.verify = AsyncMock(
            return_value=VerifyResult(is_valid=True, payer="0xP", invalid_reason=None)
        )
        mock_facilitator.settle_with_retry = AsyncMock(
            return_value=SettleResult(success=True, tx_hash="0xT", network_id="base")
        )
        mw = self._make_middleware(cfg)
        mw._facilitator = mock_facilitator
        mw._facilitator_key = "__injected__"

        sent: List[dict] = []

        async def mock_send(msg):
            sent.append(msg)

        # Use PAYMENT-SIGNATURE instead of X-PAYMENT
        scope = _make_scope(
            "/v1/chat/completions",
            headers=[(b"payment-signature", b"sig-token")],
        )
        await mw(scope, AsyncMock(), mock_send)
        # Should NOT return 402
        assert sent[0].get("type") == "http.response.start"
        assert sent[0]["status"] != 402

    @pytest.mark.asyncio
    async def test_disabled_config_passes_all_requests(self):
        cfg = _make_config(enabled=False)
        inner_called = []

        async def fake_app(scope, receive, send):
            inner_called.append(True)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = X402PaymentMiddleware(app=fake_app, config=cfg)
        await mw(
            _make_scope("/v1/chat/completions"),
            AsyncMock(),
            AsyncMock(),
        )
        assert inner_called

    @pytest.mark.asyncio
    async def test_lazy_config_none_passes_all_requests(self):
        inner_called = []

        async def fake_app(scope, receive, send):
            inner_called.append(True)

        mw = X402PaymentMiddleware(app=fake_app, get_config=lambda: None)
        await mw(
            _make_scope("/v1/chat/completions"),
            AsyncMock(),
            AsyncMock(),
        )
        assert inner_called

    @pytest.mark.asyncio
    async def test_upstream_error_skips_settle(self):
        cfg = _make_config(protected_routes=["/v1/chat/completions"])

        async def error_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 500, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mock_facilitator = AsyncMock()
        mock_facilitator.verify = AsyncMock(
            return_value=VerifyResult(is_valid=True, payer="0xP", invalid_reason=None)
        )
        mock_facilitator.settle_with_retry = AsyncMock(
            return_value=SettleResult(success=True, tx_hash="0xT", network_id="base")
        )
        mw = X402PaymentMiddleware(
            app=error_app, config=cfg, facilitator=mock_facilitator
        )

        sent: List[dict] = []

        async def mock_send(msg):
            sent.append(msg)

        scope = _make_scope(
            "/v1/chat/completions",
            headers=[(b"x-payment", b"token")],
        )
        await mw(scope, AsyncMock(), mock_send)
        await asyncio.sleep(0)

        # Upstream 500 → settle should NOT be called
        mock_facilitator.settle_with_retry.assert_not_called()

    def test_init_requires_config_or_get_config(self):
        with pytest.raises(ValueError):
            X402PaymentMiddleware(app=AsyncMock())
