"""Unit tests for the x402 pricing engine and middleware."""

import json
import math
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from litellm.proxy.x402.config import BASE_MAINNET_NETWORK, BASE_USDC_ADDRESS, USDC_SCALE, X402Config
from litellm.proxy.x402.middleware import X402Middleware, _extract_model
from litellm.proxy.x402.pricing import UsdcPricingEngine, _model_to_env_slug


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(**overrides) -> X402Config:
    """Return a minimal test config with sane defaults."""
    defaults: Dict[str, Any] = {
        "enabled": True,
        "pay_to": "0xDEADBEEF",
        "network": BASE_MAINNET_NETWORK,
        "asset": BASE_USDC_ADDRESS,
        "routes": ["/v1/chat/completions"],
        "markup_factor": 1.0,
        "usd_usdc_rate": 1.0,
        "estimated_input_tokens": 1000,
        "estimated_output_tokens": 200,
        "max_timeout_seconds": 300,
    }
    defaults.update(overrides)
    return X402Config(**defaults)


def _app_with_middleware(config: X402Config) -> Starlette:
    """Build a minimal Starlette app with X402Middleware for testing."""

    async def _handler(request):
        body = await request.body()
        return JSONResponse({"ok": True, "body": body.decode()})

    app = Starlette(routes=[Route("/v1/chat/completions", _handler, methods=["POST"])])
    app.add_middleware(X402Middleware, config=config)
    return app


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestX402ConfigFromEnv:
    def test_defaults_when_no_env_vars(self, monkeypatch):
        for key in [
            "X402_ENABLED", "X402_PAY_TO", "X402_NETWORK", "X402_ASSET",
            "X402_ROUTES", "X402_MARKUP_FACTOR", "X402_USD_USDC_RATE",
            "X402_ESTIMATED_INPUT_TOKENS", "X402_ESTIMATED_OUTPUT_TOKENS",
            "X402_MAX_TIMEOUT_SECONDS",
        ]:
            monkeypatch.delenv(key, raising=False)

        cfg = X402Config.from_env()
        assert cfg.enabled is False
        assert cfg.network == BASE_MAINNET_NETWORK
        assert cfg.asset == BASE_USDC_ADDRESS
        assert cfg.routes == ["/v1/chat/completions"]
        assert cfg.markup_factor == 1.0
        assert cfg.usd_usdc_rate == 1.0
        assert cfg.estimated_input_tokens == 1000
        assert cfg.estimated_output_tokens == 200
        assert cfg.max_timeout_seconds == 300

    def test_env_vars_override_defaults(self, monkeypatch):
        monkeypatch.setenv("X402_ENABLED", "true")
        monkeypatch.setenv("X402_PAY_TO", "0xABCD")
        monkeypatch.setenv("X402_MARKUP_FACTOR", "1.5")
        monkeypatch.setenv("X402_USD_USDC_RATE", "0.99")
        monkeypatch.setenv("X402_ROUTES", "/v1/chat/completions,/v1/completions")
        monkeypatch.setenv("X402_ESTIMATED_INPUT_TOKENS", "500")
        monkeypatch.setenv("X402_ESTIMATED_OUTPUT_TOKENS", "100")

        cfg = X402Config.from_env()
        assert cfg.enabled is True
        assert cfg.pay_to == "0xABCD"
        assert cfg.markup_factor == 1.5
        assert cfg.usd_usdc_rate == 0.99
        assert "/v1/completions" in cfg.routes
        assert cfg.estimated_input_tokens == 500
        assert cfg.estimated_output_tokens == 100


# ---------------------------------------------------------------------------
# Pricing engine tests
# ---------------------------------------------------------------------------


class TestModelToEnvSlug:
    @pytest.mark.parametrize(
        "model, expected",
        [
            ("gpt-4o", "GPT_4O"),
            ("claude-sonnet-4-5", "CLAUDE_SONNET_4_5"),
            ("deepseek/deepseek-chat", "DEEPSEEK_DEEPSEEK_CHAT"),
            ("gpt-4o-mini", "GPT_4O_MINI"),
        ],
    )
    def test_slug_conversion(self, model, expected):
        assert _model_to_env_slug(model) == expected


class TestUsdcPricingEngine:
    _FAKE_COST = {
        "gpt-4o": {
            "input_cost_per_token": 2.5e-6,
            "output_cost_per_token": 1.0e-5,
        },
        "claude-sonnet-4-5": {
            "input_cost_per_token": 3.0e-6,
            "output_cost_per_token": 1.5e-5,
        },
    }

    def _engine(self, **overrides) -> UsdcPricingEngine:
        return UsdcPricingEngine(_config(**overrides))

    def test_known_model_returns_nonzero(self):
        with patch("litellm.model_cost", self._FAKE_COST):
            engine = self._engine()
            amount = engine.compute_usdc_base_units("gpt-4o")
        assert amount > 0

    def test_gpt4o_formula_correct(self):
        # 1000 input * 2.5e-6 + 200 output * 1.0e-5 ≈ 0.0045 USD
        # * 1_000_000 ≈ 4500 base units; ceil accounts for float imprecision
        with patch("litellm.model_cost", self._FAKE_COST):
            engine = self._engine(estimated_input_tokens=1000, estimated_output_tokens=200)
            amount = engine.compute_usdc_base_units("gpt-4o")
        assert 4500 <= amount <= 4502

    def test_markup_factor_applied(self):
        with patch("litellm.model_cost", self._FAKE_COST):
            base_engine = self._engine(markup_factor=1.0, estimated_input_tokens=1000, estimated_output_tokens=200)
            markup_engine = self._engine(markup_factor=2.0, estimated_input_tokens=1000, estimated_output_tokens=200)
            base_amount = base_engine.compute_usdc_base_units("gpt-4o")
            markup_amount = markup_engine.compute_usdc_base_units("gpt-4o")
        # 2× markup should double the price (within 1 base unit of ceiling)
        assert abs(markup_amount - base_amount * 2) <= 2

    def test_usd_usdc_rate_applied(self):
        with patch("litellm.model_cost", self._FAKE_COST):
            base_engine = self._engine(usd_usdc_rate=1.0, estimated_input_tokens=1000, estimated_output_tokens=200)
            half_rate_engine = self._engine(usd_usdc_rate=0.5, estimated_input_tokens=1000, estimated_output_tokens=200)
            base_amount = base_engine.compute_usdc_base_units("gpt-4o")
            half_rate_amount = half_rate_engine.compute_usdc_base_units("gpt-4o")
        # Halving the rate doubles the USDC amount (within 1 base unit of ceiling)
        assert abs(half_rate_amount - base_amount * 2) <= 2

    def test_explicit_token_counts_override_config(self):
        with patch("litellm.model_cost", self._FAKE_COST):
            engine = self._engine()
            # 100 input * 2.5e-6 + 50 output * 1.0e-5 = 0.00025 + 0.0005 = 0.00075 USD → 750 base units
            amount = engine.compute_usdc_base_units("gpt-4o", input_tokens=100, output_tokens=50)
        assert amount == 750

    def test_unknown_model_returns_fallback(self):
        with patch("litellm.model_cost", {}):
            engine = self._engine()
            amount = engine.compute_usdc_base_units("unknown-model-xyz")
        assert amount == UsdcPricingEngine._DEFAULT_FALLBACK_BASE_UNITS

    def test_model_without_provider_prefix_lookup(self):
        cost_map = {"deepseek-chat": {"input_cost_per_token": 2.8e-7, "output_cost_per_token": 4.2e-7}}
        with patch("litellm.model_cost", cost_map):
            engine = self._engine(estimated_input_tokens=1000, estimated_output_tokens=200)
            amount = engine.compute_usdc_base_units("deepseek/deepseek-chat")
        # 1000 * 2.8e-7 + 200 * 4.2e-7 = 0.00028 + 0.000084 = 0.000364 USD → 364 base units
        assert amount == 364

    def test_env_override_bypasses_cost_table(self, monkeypatch):
        monkeypatch.setenv("X402_PRICE_GPT_4O", "99999")
        with patch("litellm.model_cost", self._FAKE_COST):
            engine = self._engine()
            amount = engine.compute_usdc_base_units("gpt-4o")
        assert amount == 99999

    def test_per_model_prices_differ(self):
        with patch("litellm.model_cost", self._FAKE_COST):
            engine = self._engine()
            gpt4o = engine.compute_usdc_base_units("gpt-4o")
            claude = engine.compute_usdc_base_units("claude-sonnet-4-5")
        # Claude input 3.0e-6 > GPT-4o input 2.5e-6 but output 1.5e-5 > 1.0e-5 too
        # → claude should be more expensive
        assert claude > gpt4o

    def test_result_always_at_least_one(self):
        # Pricing table with zero cost should still return 1
        cost_map = {"free-model": {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0}}
        with patch("litellm.model_cost", cost_map):
            engine = self._engine()
            amount = engine.compute_usdc_base_units("free-model")
        assert amount >= 1

    def test_describe_contains_model_and_usdc(self):
        engine = self._engine()
        desc = engine.describe("gpt-4o", 4500)
        assert "gpt-4o" in desc
        assert "USDC" in desc
        assert "0.004500" in desc


# ---------------------------------------------------------------------------
# _extract_model helper
# ---------------------------------------------------------------------------


class TestExtractModel:
    def test_extracts_model_field(self):
        body = json.dumps({"model": "gpt-4o", "messages": []}).encode()
        assert _extract_model(body) == "gpt-4o"

    def test_returns_none_on_empty_body(self):
        assert _extract_model(b"") is None

    def test_returns_none_on_invalid_json(self):
        assert _extract_model(b"not-json") is None

    def test_returns_none_when_model_absent(self):
        body = json.dumps({"messages": []}).encode()
        assert _extract_model(body) is None


# ---------------------------------------------------------------------------
# Middleware integration tests
# ---------------------------------------------------------------------------


class TestX402Middleware:
    def _client(self, **config_overrides) -> TestClient:
        cfg = _config(**config_overrides)
        app = _app_with_middleware(cfg)
        return TestClient(app, raise_server_exceptions=True)

    def _post(self, client: TestClient, model: str = "gpt-4o", extra_headers: Optional[Dict] = None) -> Any:
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        return client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
            headers=headers,
        )

    def test_returns_402_without_auth_or_signature(self):
        with patch("litellm.model_cost", {"gpt-4o": {"input_cost_per_token": 2.5e-6, "output_cost_per_token": 1.0e-5}}):
            client = self._client()
            resp = self._post(client)
        assert resp.status_code == 402

    def test_402_body_has_x402_version(self):
        with patch("litellm.model_cost", {}):
            client = self._client()
            resp = self._post(client)
        body = resp.json()
        assert body["x402Version"] == 1
        assert body["error"] == "Payment Required"
        assert len(body["accepts"]) == 1

    def test_402_body_has_correct_network_and_asset(self):
        with patch("litellm.model_cost", {}):
            client = self._client()
            resp = self._post(client)
        accept = resp.json()["accepts"][0]
        assert accept["network"] == BASE_MAINNET_NETWORK
        assert accept["asset"] == BASE_USDC_ADDRESS

    def test_402_body_has_pay_to(self):
        with patch("litellm.model_cost", {}):
            client = self._client(pay_to="0xRECEIVER")
            resp = self._post(client)
        accept = resp.json()["accepts"][0]
        assert accept["payTo"] == "0xRECEIVER"

    def test_402_max_amount_is_string_integer(self):
        with patch("litellm.model_cost", {}):
            client = self._client()
            resp = self._post(client)
        amount = resp.json()["accepts"][0]["maxAmountRequired"]
        assert isinstance(amount, str)
        assert int(amount) > 0

    def test_bearer_auth_bypasses_x402(self):
        with patch("litellm.model_cost", {}):
            client = self._client()
            resp = self._post(client, extra_headers={"Authorization": "Bearer sk-test-key"})
        # Should reach the handler and return 200
        assert resp.status_code == 200

    def test_payment_signature_bypasses_x402(self):
        with patch("litellm.model_cost", {}):
            client = self._client()
            resp = self._post(client, extra_headers={"X-PAYMENT-SIGNATURE": "0xsig"})
        assert resp.status_code == 200

    def test_unprotected_path_passes_through(self):
        with patch("litellm.model_cost", {}):
            cfg = _config(routes=["/v1/chat/completions"])
            app = _app_with_middleware(cfg)

            async def health(request):
                return JSONResponse({"status": "ok"})

            from starlette.routing import Route as R
            app.router.routes.append(R("/health", health))
            client = TestClient(app)
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_amount_differs_per_model(self):
        cost_map = {
            "gpt-4o": {"input_cost_per_token": 2.5e-6, "output_cost_per_token": 1.0e-5},
            "deepseek-chat": {"input_cost_per_token": 2.8e-7, "output_cost_per_token": 4.2e-7},
        }
        with patch("litellm.model_cost", cost_map):
            client = self._client()
            gpt4o_resp = self._post(client, model="gpt-4o")
            deepseek_resp = self._post(client, model="deepseek-chat")

        gpt4o_amount = int(gpt4o_resp.json()["accepts"][0]["maxAmountRequired"])
        deepseek_amount = int(deepseek_resp.json()["accepts"][0]["maxAmountRequired"])
        assert gpt4o_amount != deepseek_amount
        assert gpt4o_amount > deepseek_amount

    def test_handler_still_receives_body_with_signature(self):
        """When payment signature is present, downstream must still read the body."""
        with patch("litellm.model_cost", {}):
            client = self._client()
            resp = self._post(
                client,
                model="gpt-4o",
                extra_headers={"X-PAYMENT-SIGNATURE": "0xsig"},
            )
        data = resp.json()
        assert data["ok"] is True
        body_data = json.loads(data["body"])
        assert body_data["model"] == "gpt-4o"
