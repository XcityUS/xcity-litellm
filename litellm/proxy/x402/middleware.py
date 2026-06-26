"""x402 ASGI middleware — returns HTTP 402 when payment is required.

Protocol summary (x402 v1)
--------------------------
1. Client sends a request *without* a payment signature.
2. Server responds 402 with a ``PaymentRequired`` JSON body listing USDC
   price, recipient address, network, and asset contract.
3. Client signs the payment with their EVM wallet and re-sends the request
   with an ``X-PAYMENT-SIGNATURE`` header.
4. Server verifies the signature via a Facilitator (XCT-144) and processes
   the request.

This middleware handles step 2.  Steps 3–4 are implemented in XCT-144.

Pass-through rules
------------------
- Paths not in ``config.routes``: always pass through.
- Requests with ``Authorization: Bearer ...``: pass through (existing API-key
  auth is a parallel channel, not replaced by x402).
- Requests with ``X-PAYMENT-SIGNATURE`` header: pass through for upstream
  verification (XCT-144).
- All other requests to protected paths: return 402.
"""

import json
import logging
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from litellm.proxy.x402.config import X402Config
from litellm.proxy.x402.pricing import UsdcPricingEngine

logger = logging.getLogger(__name__)

_PAYMENT_SIG_HEADER = "x-payment-signature"
_DEFAULT_MODEL = "gpt-4o"


class X402Middleware:
    """ASGI middleware enforcing x402 payment on configured routes.

    Add to a FastAPI/Starlette app with::

        app.add_middleware(X402Middleware, config=X402Config.from_env())

    The middleware reads the request body to extract the model name so it can
    compute the per-model USDC price.  The body is buffered and re-injected
    into the ASGI receive channel so the downstream handler can still read it.
    """

    def __init__(self, app: ASGIApp, config: X402Config) -> None:
        self._app = app
        self._config = config
        self._pricing = UsdcPricingEngine(config)
        self._protected = frozenset(config.routes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path not in self._protected:
            await self._app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

        # Existing API-key auth bypasses x402 (parallel payment channel)
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            await self._app(scope, receive, send)
            return

        # Payment signature present → pass through for Facilitator verification (XCT-144)
        if _PAYMENT_SIG_HEADER in headers:
            await self._app(scope, receive, send)
            return

        # No auth and no payment: read body, compute price, return 402
        body, replay_receive = await _buffer_body(receive)
        model = _extract_model(body) or _DEFAULT_MODEL

        response = self._build_402(scope, model)
        await response(scope, replay_receive, send)

    def _build_402(self, scope: Scope, model: str) -> JSONResponse:
        usdc_base_units = self._pricing.compute_usdc_base_units(model)

        # Build the resource URL from scope
        scheme = scope.get("scheme", "https")
        server = scope.get("server")
        host = f"{server[0]}:{server[1]}" if server else "localhost"
        path = scope.get("path", "/")
        resource = f"{scheme}://{host}{path}"

        description = self._pricing.describe(model, usdc_base_units)

        body = {
            "x402Version": 1,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": self._config.network,
                    "maxAmountRequired": str(usdc_base_units),
                    "resource": resource,
                    "description": description,
                    "mimeType": "application/json",
                    "payTo": self._config.pay_to,
                    "maxTimeoutSeconds": self._config.max_timeout_seconds,
                    "asset": self._config.asset,
                    "extra": {
                        "name": "USDC",
                        "version": "2",
                    },
                }
            ],
            "error": "Payment Required",
        }

        logger.debug(
            "x402: 402 for path=%s model=%s amount=%d base_units",
            path,
            model,
            usdc_base_units,
        )
        return JSONResponse(status_code=402, content=body)


async def _buffer_body(receive: Receive):
    """Read the full request body and return a replay receive callable.

    Returns (body_bytes, new_receive) where new_receive replays the body
    so the downstream handler can still read it.
    """
    chunks = []
    more_body = True
    while more_body:
        message = await receive()
        chunks.append(message.get("body", b""))
        more_body = message.get("more_body", False)

    body = b"".join(chunks)

    consumed = False

    async def replay() -> dict:
        nonlocal consumed
        if not consumed:
            consumed = True
            return {"type": "http.request", "body": body, "more_body": False}
        # Subsequent calls (e.g. streaming): return empty
        return {"type": "http.request", "body": b"", "more_body": False}

    return body, replay


def _extract_model(body: bytes) -> Optional[str]:
    """Parse JSON body and extract the ``model`` field."""
    if not body:
        return None
    try:
        data = json.loads(body)
        model = data.get("model")
        return str(model) if model else None
    except (json.JSONDecodeError, AttributeError):
        return None
