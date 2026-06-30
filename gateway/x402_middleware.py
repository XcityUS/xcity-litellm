"""x402 pay-per-request middleware for the XCity LLM gateway.

Adds pay-per-request support via the x402 protocol on configured routes.
Runs as a parallel auth channel — requests that already carry an
``Authorization: Bearer <key>`` header are passed through to the existing
LiteLLM auth flow unchanged.  Only requests with neither a valid API key
nor an ``X-PAYMENT`` header are intercepted.

Protocol flow:
  1. Request arrives on an x402 route with no auth →
     gateway returns 402 + PaymentRequired JSON body.
  2. Client pays on-chain and re-sends with ``X-PAYMENT`` header.
  3. Middleware calls the configured facilitator to verify.
  4. Facilitator says valid → request passes to LiteLLM proxy;
     ``X-PAYMENT-RESPONSE`` is attached to the upstream response.
  5. Facilitator says invalid → 402 again.

Configuration (environment variables)
--------------------------------------
X402_ENABLED           "true" to enable; anything else (default) disables.
X402_PAYMENT_ADDRESS   EVM address that receives USDC (required when enabled).
X402_AMOUNT_USDC       Amount in USDC per request (default: "0.01").
X402_FACILITATOR_URL   Verify endpoint of the x402 facilitator
                       (default: Coinbase-hosted https://x402.org/facilitator).
X402_ROUTES            Comma-separated paths subject to x402 gating
                       (default: "/v1/chat/completions").
X402_RESOURCE_URL      Base URL exposed to clients in the PaymentRequired body,
                       e.g. "https://api.xcity.us"
                       (default: "" — path is used bare).
X402_INTERNAL_KEY      LiteLLM API key injected as the Bearer token after
                       successful payment so the request passes auth downstream.
                       Should be a virtual key created in LiteLLM with an
                       appropriate budget/rate-limit (default: LITELLM_MASTER_KEY).
"""

import base64
import json
import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# x402 protocol constants
_X402_VERSION = 1
_NETWORK = "eip155:8453"  # Base mainnet (CAIP-2)
_ASSET_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_USDC_DECIMALS = 6
_MAX_TIMEOUT_SECONDS = 300

# Default facilitator operated by Coinbase
_DEFAULT_FACILITATOR_URL = "https://x402.org/facilitator"

_PAYMENT_HEADER = "x-payment"
_PAYMENT_RESPONSE_HEADER = "x-payment-response"


def _usdc_to_atomic(amount_usdc: str) -> str:
    """Convert a human-readable USDC amount to atomic units (6 decimal places)."""
    try:
        val = Decimal(amount_usdc) * Decimal(10**_USDC_DECIMALS)
        return str(int(val))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid X402_AMOUNT_USDC value: {amount_usdc!r}") from exc


def _parse_routes(raw: str) -> frozenset:
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


class X402Middleware:
    """ASGI middleware that gates configured routes behind x402 micropayments.

    When disabled (``X402_ENABLED != "true"``) the middleware is a no-op
    transparent pass-through so the gateway can be deployed without any
    blockchain dependencies.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app
        self._enabled = os.environ.get("X402_ENABLED", "false").lower() == "true"

        if not self._enabled:
            return

        raw_address = os.environ.get("X402_PAYMENT_ADDRESS", "")
        if not raw_address:
            raise RuntimeError(
                "X402_PAYMENT_ADDRESS must be set when X402_ENABLED=true"
            )

        self._payment_address: str = raw_address
        self._amount_atomic: str = _usdc_to_atomic(
            os.environ.get("X402_AMOUNT_USDC", "0.01")
        )
        self._facilitator_url: str = os.environ.get(
            "X402_FACILITATOR_URL", _DEFAULT_FACILITATOR_URL
        ).rstrip("/")
        self._routes: frozenset = _parse_routes(
            os.environ.get("X402_ROUTES", "/v1/chat/completions")
        )
        self._resource_base: str = os.environ.get("X402_RESOURCE_URL", "").rstrip("/")
        # Key injected into the request after successful payment so the downstream
        # LiteLLM auth layer accepts it.  Falls back to LITELLM_MASTER_KEY.
        self._internal_key: str = os.environ.get(
            "X402_INTERNAL_KEY",
            os.environ.get("LITELLM_MASTER_KEY", ""),
        )

        logger.info(
            "x402 middleware enabled — routes=%s amount_usdc=%s payTo=%s",
            sorted(self._routes),
            os.environ.get("X402_AMOUNT_USDC", "0.01"),
            self._payment_address,
        )

    # ------------------------------------------------------------------
    # ASGI entry point
    # ------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._enabled or scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path not in self._routes:
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)

        # Parallel channel: requests with an API key skip x402 entirely.
        if headers.get("authorization", "").startswith("Bearer "):
            await self._app(scope, receive, send)
            return

        raw_payment = headers.get(_PAYMENT_HEADER)
        if not raw_payment:
            resp = self._build_402(path)
            await resp(scope, receive, send)
            return

        try:
            payment_payload = _decode_payment_header(raw_payment)
        except (ValueError, json.JSONDecodeError):
            logger.debug("Malformed X-Payment header received")
            resp = JSONResponse(
                {"error": "Malformed X-Payment header — expected base64 or JSON"},
                status_code=400,
            )
            await resp(scope, receive, send)
            return

        payment_req = self._payment_requirements(path)
        is_valid, reason, settle_receipt = await self._verify(
            payment_payload, payment_req
        )
        if not is_valid:
            resp = self._build_402(path, error=reason)
            await resp(scope, receive, send)
            return

        # Payment is valid.  Inject the internal key so the downstream LiteLLM
        # auth layer treats this as an authenticated request.
        if self._internal_key:
            scope = _inject_bearer(scope, self._internal_key)

        # Forward to upstream and attach settlement header.
        settlement_b64 = _encode_b64(settle_receipt) if settle_receipt else None
        await _forward_with_settlement(
            self._app, scope, receive, send, settlement_b64
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resource_url(self, path: str) -> str:
        if self._resource_base:
            return f"{self._resource_base}{path}"
        return path

    def _payment_requirements(self, path: str) -> dict:
        return {
            "scheme": "exact",
            "network": _NETWORK,
            "maxAmountRequired": self._amount_atomic,
            "resource": self._resource_url(path),
            "description": "XCity AI API — pay-per-request via x402",
            "mimeType": "application/json",
            "payTo": self._payment_address,
            "maxTimeoutSeconds": _MAX_TIMEOUT_SECONDS,
            "asset": _ASSET_USDC,
            "extra": {
                "name": "USD Coin",
                "version": "2",
            },
        }

    def _build_402(self, path: str, error: Optional[str] = None) -> JSONResponse:
        body = {
            "x402Version": _X402_VERSION,
            "accepts": [self._payment_requirements(path)],
            "error": error or "Payment required",
        }
        return JSONResponse(
            body,
            status_code=402,
            headers={_PAYMENT_RESPONSE_HEADER: _encode_b64(body)},
        )

    async def _verify(
        self,
        payment_payload: dict,
        payment_req: dict,
    ) -> tuple:
        """Call the facilitator and return ``(is_valid, reason, settle_receipt)``."""
        verify_body = {
            "x402Version": _X402_VERSION,
            "payload": payment_payload,
            "paymentRequirements": payment_req,
        }
        verify_url = f"{self._facilitator_url}/verify"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(verify_url, json=verify_body)
        except httpx.RequestError as exc:
            logger.warning("x402 facilitator unreachable: %s", exc)
            return False, f"Facilitator unreachable: {exc}", None

        if resp.status_code != 200:
            logger.warning(
                "x402 facilitator returned HTTP %s", resp.status_code
            )
            return False, f"Facilitator error {resp.status_code}", None

        data = resp.json()
        if data.get("isValid"):
            return True, None, data
        reason = data.get("invalidReason") or "Payment invalid"
        return False, reason, None


# ------------------------------------------------------------------
# Module-level utilities
# ------------------------------------------------------------------


def _inject_bearer(scope: Scope, api_key: str) -> Scope:
    """Return a copy of *scope* with an ``Authorization: Bearer`` header set."""
    auth_value = f"Bearer {api_key}".encode()
    existing = [
        (k, v)
        for k, v in scope.get("headers", [])
        if k.lower() != b"authorization"
    ]
    return {**scope, "headers": [(b"authorization", auth_value), *existing]}


def _decode_payment_header(raw: str) -> dict:
    """Accept both raw JSON and base64-encoded JSON in X-Payment."""
    raw = raw.strip()
    try:
        decoded_bytes = base64.b64decode(raw, validate=True)
        return json.loads(decoded_bytes)
    except Exception:
        return json.loads(raw)


def _encode_b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


async def _forward_with_settlement(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    settlement_b64: Optional[str],
) -> None:
    """Pass the request to *app* and inject the settlement header into the response."""
    if settlement_b64 is None:
        await app(scope, receive, send)
        return

    async def _send_with_header(message: dict) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            headers.append(
                (
                    _PAYMENT_RESPONSE_HEADER.encode(),
                    settlement_b64.encode(),
                )
            )
            message = {**message, "headers": headers}
        await send(message)

    await app(scope, receive, _send_with_header)
