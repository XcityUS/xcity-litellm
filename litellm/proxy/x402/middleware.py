"""
ASGI middleware for x402 HTTP Payment Required support.

Flow per request on a protected route:
  1. Check for X-PAYMENT (or PAYMENT-SIGNATURE) header.
  2. If absent → respond 402 with payment requirements JSON.
  3. If present → call facilitator /verify.
  4. If invalid → respond 402.
  5. If valid  → forward to application.
  6. On 2xx response → fire-and-forget facilitator /settle (with retry).

Configuration is read from the proxy YAML under the ``x402`` key.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

from litellm.proxy.x402.facilitator import BaseFacilitatorClient, build_facilitator

logger = logging.getLogger(__name__)

# Routes protected by default when no explicit list is provided.
_DEFAULT_PROTECTED_ROUTES = [
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/v1/images/generations",
    "/v1/audio/speech",
    "/v1/audio/transcriptions",
]

# x402 protocol version
_X402_VERSION = 1


@dataclass
class X402Config:
    """Runtime configuration for the x402 middleware."""

    enabled: bool = False

    # Facilitator selection
    facilitator: str = "coinbase_cdp"
    facilitator_url: Optional[str] = None
    api_key: Optional[str] = None

    # Payment requirements advertised in 402 responses
    pay_to: str = ""  # EVM wallet address to receive USDC
    amount: str = "0"  # USDC in base units (1_000_000 = $1.00)
    asset: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base mainnet
    network: str = "base"
    description: str = "Pay to access LiteLLM API"
    max_timeout_seconds: int = 300

    # Which header carries the payment payload
    # "X-PAYMENT" is the x402 standard; "PAYMENT-SIGNATURE" is an alias
    payment_header: str = "X-PAYMENT"

    # Routes that require payment (case-sensitive path prefixes).
    # Empty → use _DEFAULT_PROTECTED_ROUTES.
    protected_routes: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "X402Config":
        """Build an X402Config from the ``x402`` section of the proxy YAML."""
        return cls(
            enabled=bool(raw.get("enabled", False)),
            facilitator=str(raw.get("facilitator", "coinbase_cdp")),
            facilitator_url=raw.get("facilitator_url") or None,
            api_key=raw.get("api_key") or None,
            pay_to=str(raw.get("pay_to", "")),
            amount=str(raw.get("amount", "0")),
            asset=str(
                raw.get(
                    "asset",
                    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                )
            ),
            network=str(raw.get("network", "base")),
            description=str(raw.get("description", "Pay to access LiteLLM API")),
            max_timeout_seconds=int(raw.get("max_timeout_seconds", 300)),
            payment_header=str(raw.get("payment_header", "X-PAYMENT")),
            protected_routes=list(raw.get("protected_routes", [])),
        )

    def payment_requirements(self, resource: str) -> Dict[str, Any]:
        """Build the paymentRequirements dict for a given resource URL."""
        return {
            "scheme": "exact",
            "network": self.network,
            "maxAmountRequired": self.amount,
            "resource": resource,
            "description": self.description,
            "mimeType": "application/json",
            "payTo": self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "asset": self.asset,
            "outputSchema": None,
            "extra": None,
        }

    def x402_body(self, resource: str) -> bytes:
        """Serialise the x402 payment-required response body."""
        payload = {
            "x402Version": _X402_VERSION,
            "error": "X-PAYMENT header is required",
            "accepts": [self.payment_requirements(resource)],
        }
        return json.dumps(payload).encode()


class X402PaymentMiddleware:
    """
    ASGI middleware that enforces x402 payment requirements on protected routes.

    Accepts either a static config or a callable that returns Optional[X402Config]
    (the callable form lets the middleware be registered at import time, before the
    proxy YAML is loaded):

        # Static (config known at registration time)
        app.add_middleware(X402PaymentMiddleware, config=X402Config(...))

        # Lazy (config resolved from a global at request time)
        _x402_config: Optional[X402Config] = None
        app.add_middleware(X402PaymentMiddleware, get_config=lambda: _x402_config)
    """

    def __init__(
        self,
        app: ASGIApp,
        config: Optional[X402Config] = None,
        get_config=None,
        facilitator: Optional[BaseFacilitatorClient] = None,
    ) -> None:
        if config is None and get_config is None:
            raise ValueError("Provide either config= or get_config=")
        self.app = app
        self._static_config = config
        self._get_config = get_config
        # Facilitator is created lazily on first use so that get_config() has
        # time to be populated before the first request arrives.
        # Pass facilitator= in tests to inject a mock without hitting the network.
        self._facilitator: Optional[BaseFacilitatorClient] = facilitator
        self._facilitator_key: Optional[str] = (
            "__injected__" if facilitator is not None else None
        )

    # ------------------------------------------------------------------
    # ASGI interface
    # ------------------------------------------------------------------

    def _resolve_config(self) -> Optional[X402Config]:
        if self._static_config is not None:
            return self._static_config
        return self._get_config() if self._get_config is not None else None

    def _ensure_facilitator(self, config: X402Config) -> BaseFacilitatorClient:
        # Injected facilitator (e.g. in tests) is never rebuilt
        if self._facilitator_key == "__injected__":
            assert self._facilitator is not None
            return self._facilitator
        # Rebuild facilitator if config identity changed (e.g. hot-reload)
        key = f"{config.facilitator}|{config.facilitator_url}|{config.api_key}"
        if self._facilitator is None or self._facilitator_key != key:
            self._facilitator = build_facilitator(
                facilitator_type=config.facilitator,
                base_url=config.facilitator_url,
                api_key=config.api_key,
            )
            self._facilitator_key = key
        return self._facilitator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        config = self._resolve_config()
        if config is None or not config.enabled:
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        protected = config.protected_routes or _DEFAULT_PROTECTED_ROUTES
        if not any(path.startswith(p) for p in protected):
            await self.app(scope, receive, send)
            return

        facilitator = self._ensure_facilitator(config)
        payment_header_key = config.payment_header.lower().encode()
        headers: Dict[bytes, bytes] = {
            k.lower(): v for k, v in scope.get("headers", [])
        }
        payment = (
            (
                headers.get(payment_header_key, b"")
                or headers.get(b"payment-signature", b"")
            )
            .decode("utf-8", errors="replace")
            .strip()
        )

        resource = self._resource_url(scope)
        requirements = config.payment_requirements(resource)

        if not payment:
            await self._send_402(send, config.x402_body(resource))
            return

        # Verify payment signature with the facilitator
        try:
            verify_result = await facilitator.verify(payment, requirements)
        except Exception as exc:
            logger.error("x402 verify failed, rejecting request: %s", exc)
            await self._send_402(send, config.x402_body(resource))
            return

        if not verify_result.is_valid:
            logger.warning(
                "x402 verify rejected: payer=%s reason=%s",
                verify_result.payer,
                verify_result.invalid_reason,
            )
            body = json.dumps(
                {
                    "x402Version": _X402_VERSION,
                    "error": verify_result.invalid_reason or "payment invalid",
                    "accepts": [requirements],
                }
            ).encode()
            await self._send_402(send, body)
            return

        logger.info("x402 payment verified for payer=%s", verify_result.payer)

        # Track the HTTP status to decide whether to settle
        response_status: List[int] = []

        async def capturing_send(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                response_status.append(message.get("status", 0))
            await send(message)

        await self.app(scope, receive, capturing_send)

        status = response_status[0] if response_status else 0
        if 200 <= status < 300:
            asyncio.create_task(
                self._settle_background(facilitator, payment, requirements, status)
            )
        else:
            logger.info("x402 skipping settle: upstream returned HTTP %d", status)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resource_url(scope: Scope) -> str:
        """Reconstruct a best-effort absolute URL for the request."""
        scheme = scope.get("scheme", "https")
        server = scope.get("server")
        host = f"{server[0]}:{server[1]}" if server else "localhost"
        path = scope.get("path", "/")
        qs = scope.get("query_string", b"")
        qs_str = f"?{qs.decode()}" if qs else ""
        return f"{scheme}://{host}{path}{qs_str}"

    @staticmethod
    async def _send_402(send: Send, body: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 402,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _settle_background(
        self,
        facilitator: BaseFacilitatorClient,
        payment: str,
        requirements: Dict[str, Any],
        upstream_status: int,
    ) -> None:
        logger.info(
            "x402 starting background settle (upstream_status=%d)", upstream_status
        )
        result = await facilitator.settle_with_retry(payment, requirements)
        if result.success:
            logger.info(
                "x402 settle complete: tx_hash=%s network=%s",
                result.tx_hash,
                result.network_id,
            )
        else:
            logger.error("x402 settle permanently failed: error=%s", result.error)
