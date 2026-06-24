"""
x402 multichain payment middleware for the XCity LiteLLM proxy.

Implements the x402 payment protocol (https://github.com/coinbase/x402).
Supports Base (eip155:8453), Arbitrum (eip155:42161), and Solana mainnet
(solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp).

Environment variables
---------------------
X402_ENABLED               Set to "true" to enable the payment gate (default: false)
X402_RESOURCE_URL          Base URL advertised in accepts[] (e.g. https://api.xcity.ai)
X402_AMOUNT_USDC           Required payment in USDC micro-units (default: 1000000 = $1.00)
X402_PROXY_API_KEY         LiteLLM API key injected into verified requests for downstream auth
X402_GATED_PATHS           Comma-separated path prefixes to gate (default: /v1/chat/completions,…)

Per-chain (replace CHAIN with BASE | ARBITRUM | SOLANA):
PAYMENT_ADDRESS_CHAIN      Recipient wallet / SPL token account on that chain
FACILITATOR_CHAIN_URL      x402 facilitator base URL for verify + settle on that chain
"""

import asyncio
import base64
import json
import logging
import os
from typing import Dict, List, Optional, Tuple

from starlette.types import ASGIApp, Receive, Scope, Send

from litellm.types.x402 import (
    EVM_NETWORKS,
    NETWORK_ARBITRUM,
    NETWORK_BASE,
    NETWORK_SOLANA,
    SOLANA_NETWORKS,
    USDC_CONTRACT_ARBITRUM,
    USDC_CONTRACT_BASE,
    USDC_MINT_SOLANA,
    X402PaymentAccept,
    X402PaymentPayload,
    X402Response,
    X402SettlementReceipt,
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default gated path prefixes (applied when X402_GATED_PATHS is not set)
# ---------------------------------------------------------------------------

_DEFAULT_GATED_PREFIXES = [
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/v1/messages",
]


# ---------------------------------------------------------------------------
# Pure-Python base58 codec (no extra dependency required for Solana addresses)
# ---------------------------------------------------------------------------

_BASE58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes:
    alphabet = _BASE58_ALPHABET
    num = 0
    for char in s.encode("ascii"):
        num = num * 58 + alphabet.index(char)
    result: List[int] = []
    while num > 0:
        result.append(num % 256)
        num //= 256
    leading_zeros = len(s) - len(s.lstrip("1"))
    return bytes([0] * leading_zeros + result[::-1])


# ---------------------------------------------------------------------------
# Signature verification helpers
# ---------------------------------------------------------------------------


def _verify_evm_payment(
    payment: X402PaymentPayload, chain_config: X402PaymentAccept
) -> None:
    """
    Structural validation for EVM (secp256k1) payments.

    Full EIP-712 / EIP-3009 signature recovery requires eth_account (not in
    the dependency tree).  We validate structure and destination address, then
    rely on the facilitator for cryptographic verification.  When no facilitator
    is configured this still prevents obviously malformed payloads from passing.
    """
    inner = payment.payload
    sig: str = inner.get("signature", "")
    auth: Dict = inner.get("authorization", {})

    if not sig.startswith("0x") or len(sig) != 132:
        raise ValueError(
            f"EVM signature must be 0x + 65 bytes hex, got length {len(sig)}"
        )

    from_addr: str = auth.get("from", "")
    to_addr: str = auth.get("to", "")

    if not from_addr or not to_addr:
        raise ValueError("Missing 'from' or 'to' in EVM authorization")

    if to_addr.lower() != chain_config.payTo.lower():
        raise ValueError(
            f"Payment destination {to_addr!r} does not match configured payTo {chain_config.payTo!r}"
        )


def _verify_solana_payment(
    payment: X402PaymentPayload, chain_config: X402PaymentAccept
) -> None:
    """
    Verify a Solana Ed25519 signature over the authorization payload.

    The message is the canonical JSON of the ``authorization`` dict (keys
    sorted, no extra whitespace) — matching the x402 Solana client convention.

    Requires the ``cryptography`` package (already a proxy dependency).
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    inner = payment.payload
    sig_b58: str = inner.get("signature", "")
    auth: Dict = inner.get("authorization", {})
    pubkey_b58: str = auth.get("from", "")

    if not pubkey_b58:
        raise ValueError("Missing Solana public key in authorization.from")
    if not sig_b58:
        raise ValueError("Missing Solana signature")

    to_account: str = auth.get("to", "")
    if to_account != chain_config.payTo:
        raise ValueError(
            f"Solana recipient {to_account!r} does not match configured payTo {chain_config.payTo!r}"
        )

    try:
        pubkey_bytes = _b58decode(pubkey_b58)
        sig_bytes = _b58decode(sig_b58)
    except Exception as exc:
        raise ValueError(f"base58 decode failed: {exc}") from exc

    message = json.dumps(auth, sort_keys=True, separators=(",", ":")).encode("utf-8")

    try:
        public_key = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
        public_key.verify(sig_bytes, message)
    except InvalidSignature as exc:
        raise ValueError("Solana Ed25519 signature verification failed") from exc
    except Exception as exc:
        raise ValueError(f"Solana signature verification error: {exc}") from exc


def verify_payment_local(
    payment: X402PaymentPayload, chain_config: X402PaymentAccept
) -> None:
    """
    Dispatch to the correct local signature verifier based on network type.

    EVM networks use secp256k1 (structural check only — full EIP-712 recovery
    requires eth_account which is not in the dependency tree; use a facilitator
    for production EVM payments).

    Solana uses Ed25519 (cryptographic verification via the ``cryptography`` lib).
    """
    network = payment.network
    if network in EVM_NETWORKS:
        _verify_evm_payment(payment, chain_config)
    elif network in SOLANA_NETWORKS:
        _verify_solana_payment(payment, chain_config)
    else:
        raise ValueError(f"Unsupported payment network: {network!r}")


# ---------------------------------------------------------------------------
# Facilitator HTTP helpers
# ---------------------------------------------------------------------------


async def _call_facilitator(
    url: str,
    endpoint: str,
    payment_payload: dict,
    payment_requirements: dict,
    timeout: float,
) -> dict:
    import httpx

    body = {
        "x402Version": 1,
        "paymentPayload": payment_payload,
        "paymentRequirements": payment_requirements,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{url.rstrip('/')}/{endpoint}", json=body)
        resp.raise_for_status()
        return resp.json()


async def facilitator_verify(
    url: str, payment_payload: dict, payment_requirements: dict
) -> Tuple[bool, Optional[str]]:
    """Return (is_valid, invalid_reason)."""
    try:
        result = await _call_facilitator(
            url, "verify", payment_payload, payment_requirements, timeout=10.0
        )
        return result.get("isValid", False), result.get("invalidReason")
    except Exception as exc:
        _logger.warning("x402 facilitator verify error (%s): %s", url, exc)
        return False, str(exc)


async def facilitator_settle(
    url: str, payment_payload: dict, payment_requirements: dict
) -> dict:
    """Return the settle response dict (may include txHash on success)."""
    try:
        return await _call_facilitator(
            url, "settle", payment_payload, payment_requirements, timeout=30.0
        )
    except Exception as exc:
        _logger.warning("x402 facilitator settle error (%s): %s", url, exc)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Chain configuration builder
# ---------------------------------------------------------------------------


def _build_chain_configs(
    resource_url: str, amount_usdc: str
) -> List[X402PaymentAccept]:
    """
    Read per-chain env vars and return the list of payment options to
    advertise in a 402 response.  Only chains with PAYMENT_ADDRESS_* set
    are included.
    """
    configs: List[X402PaymentAccept] = []

    pay_base = os.environ.get("PAYMENT_ADDRESS_BASE", "")
    if pay_base:
        configs.append(
            X402PaymentAccept(
                scheme="exact",
                network=NETWORK_BASE,
                maxAmountRequired=amount_usdc,
                resource=resource_url,
                description="XCity API payment — Base USDC",
                payTo=pay_base,
                asset=USDC_CONTRACT_BASE,
                extra={"name": "USDC", "version": "2"},
            )
        )

    pay_arb = os.environ.get("PAYMENT_ADDRESS_ARBITRUM", "")
    if pay_arb:
        configs.append(
            X402PaymentAccept(
                scheme="exact",
                network=NETWORK_ARBITRUM,
                maxAmountRequired=amount_usdc,
                resource=resource_url,
                description="XCity API payment — Arbitrum USDC",
                payTo=pay_arb,
                asset=USDC_CONTRACT_ARBITRUM,
                extra={"name": "USDC", "version": "2"},
            )
        )

    pay_sol = os.environ.get("PAYMENT_ADDRESS_SOLANA", "")
    if pay_sol:
        configs.append(
            X402PaymentAccept(
                scheme="exact",
                network=NETWORK_SOLANA,
                maxAmountRequired=amount_usdc,
                resource=resource_url,
                description="XCity API payment — Solana USDC",
                payTo=pay_sol,
                asset=USDC_MINT_SOLANA,
                extra={"name": "USDC", "decimals": 6},
            )
        )

    return configs


def _facilitator_url_for(network: str) -> Optional[str]:
    if network == NETWORK_BASE:
        return os.environ.get("FACILITATOR_BASE_URL") or None
    if network == NETWORK_ARBITRUM:
        return os.environ.get("FACILITATOR_ARBITRUM_URL") or None
    if network in SOLANA_NETWORKS:
        return os.environ.get("FACILITATOR_SOLANA_URL") or None
    return None


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------


class X402PaymentMiddleware:
    """
    Pure-ASGI middleware that gates LLM inference endpoints behind x402 payments.

    Flow
    ----
    1. If X402_ENABLED != "true" or the path is not gated → pass through.
    2. If no PAYMENT_ADDRESS_* env vars are set → pass through (unconfigured).
    3. If ``X-PAYMENT`` header is absent → return 402 with the ``accepts[]``
       multi-chain payload.  The ``Accept`` header's ``network=`` parameter is
       used to order the list (preferred network first).
    4. Decode the ``X-PAYMENT`` base64 JSON payload and identify the network.
    5. If a facilitator URL is configured for that network → call
       ``/verify`` on the facilitator.  If not configured → run local
       signature validation (full Ed25519 for Solana; structural for EVM).
    6. On success → inject ``Authorization: Bearer {X402_PROXY_API_KEY}``
       (so downstream LiteLLM auth passes) and forward the request.
    7. After the LLM response body is sent → fire-and-forget ``/settle``
       on the facilitator (if configured) and attach ``X-PAYMENT-RESPONSE``
       to the response headers.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    # ------------------------------------------------------------------
    # Configuration readers (read env on every call so hot-reload works)
    # ------------------------------------------------------------------

    @staticmethod
    def _enabled() -> bool:
        return os.environ.get("X402_ENABLED", "false").lower() == "true"

    @staticmethod
    def _gated_prefixes() -> List[str]:
        raw = os.environ.get("X402_GATED_PATHS", "")
        if raw:
            return [p.strip() for p in raw.split(",") if p.strip()]
        return _DEFAULT_GATED_PREFIXES

    @staticmethod
    def _resource_url() -> str:
        return os.environ.get("X402_RESOURCE_URL", "https://api.xcity.ai")

    @staticmethod
    def _amount_usdc() -> str:
        return os.environ.get("X402_AMOUNT_USDC", "1000000")

    @staticmethod
    def _proxy_api_key() -> Optional[str]:
        return os.environ.get("X402_PROXY_API_KEY") or None

    # ------------------------------------------------------------------
    # ASGI helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _send_json_response(send: Send, status: int, body_dict: dict) -> None:
        body = json.dumps(body_dict, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    @staticmethod
    def _parse_preferred_network(
        headers: Dict[bytes, bytes], available: List[str]
    ) -> Optional[str]:
        """
        Parse ``Accept: application/x402+json; network=<caip2>`` and return
        the preferred network if it appears in ``available``.
        """
        accept = headers.get(b"accept", b"").decode("utf-8", errors="replace")
        for part in accept.split(";"):
            part = part.strip()
            if part.startswith("network="):
                preferred = part[len("network=") :]
                if preferred in available:
                    return preferred
        return None

    # ------------------------------------------------------------------
    # Core ASGI __call__
    # ------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._enabled():
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        gated = self._gated_prefixes()
        if not any(path.startswith(p) for p in gated):
            await self.app(scope, receive, send)
            return

        chain_configs = _build_chain_configs(self._resource_url(), self._amount_usdc())
        if not chain_configs:
            # Payment gate is enabled but no addresses are configured → pass through
            _logger.debug(
                "x402: enabled but no PAYMENT_ADDRESS_* vars set, passing through"
            )
            await self.app(scope, receive, send)
            return

        headers: Dict[bytes, bytes] = dict(scope.get("headers", []))
        available_networks = [c.network for c in chain_configs]

        x_payment_raw: Optional[bytes] = headers.get(b"x-payment")
        if not x_payment_raw:
            # Optionally re-order accepts by client preference
            preferred = self._parse_preferred_network(headers, available_networks)
            if preferred:
                chain_configs = sorted(
                    chain_configs, key=lambda c: 0 if c.network == preferred else 1
                )
            payload = X402Response(
                accepts=chain_configs,
                error="Payment required",
            )
            await self._send_json_response(send, 402, payload.model_dump())
            return

        # Decode X-PAYMENT header
        try:
            decoded = json.loads(base64.b64decode(x_payment_raw).decode("utf-8"))
            payment = X402PaymentPayload.model_validate(decoded)
        except Exception as exc:
            _logger.debug("x402: malformed X-PAYMENT header: %s", exc)
            await self._send_json_response(
                send, 402, {"error": "Malformed X-PAYMENT header", "x402Version": 1}
            )
            return

        if payment.network not in available_networks:
            await self._send_json_response(
                send,
                402,
                {
                    "error": f"Network {payment.network!r} is not supported",
                    "x402Version": 1,
                    "accepts": [c.model_dump() for c in chain_configs],
                },
            )
            return

        chain_config = next(c for c in chain_configs if c.network == payment.network)
        payment_payload_dict = decoded  # raw dict for facilitator calls

        # Verify payment
        facilitator_url = _facilitator_url_for(payment.network)
        if facilitator_url:
            is_valid, reason = await facilitator_verify(
                facilitator_url, payment_payload_dict, chain_config.model_dump()
            )
            if not is_valid:
                await self._send_json_response(
                    send,
                    402,
                    {
                        "error": reason or "Payment verification failed",
                        "x402Version": 1,
                    },
                )
                return
        else:
            try:
                verify_payment_local(payment, chain_config)
            except ValueError as exc:
                await self._send_json_response(
                    send, 402, {"error": str(exc), "x402Version": 1}
                )
                return

        # Payment verified — inject downstream auth key if configured
        proxy_api_key = self._proxy_api_key()
        if proxy_api_key:
            new_headers = [
                (k, v)
                for k, v in scope.get("headers", [])
                if k.lower() != b"authorization"
            ]
            new_headers.append(
                (b"authorization", f"Bearer {proxy_api_key}".encode("utf-8"))
            )
            scope = {**scope, "headers": new_headers}

        # Identify payer address for receipt
        payer: Optional[str] = payment.payload.get("authorization", {}).get("from")

        # Settlement result (populated asynchronously after response)
        settle_result: dict = {}
        response_started = False

        async def wrapped_send(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                receipt = X402SettlementReceipt(
                    success=True,
                    network=payment.network,
                    txHash=settle_result.get("txHash"),
                    payer=payer,
                )
                hdrs = list(message.get("headers", []))
                hdrs.append(
                    (
                        b"x-payment-response",
                        json.dumps(receipt.model_dump(), separators=(",", ":")).encode(
                            "utf-8"
                        ),
                    )
                )
                await send({**message, "headers": hdrs})
            else:
                await send(message)

        await self.app(scope, receive, wrapped_send)

        # Fire-and-forget settlement after the response has been sent
        if facilitator_url and response_started:
            asyncio.create_task(
                _settle_background(
                    facilitator_url,
                    payment_payload_dict,
                    chain_config.model_dump(),
                    settle_result,
                )
            )


async def _settle_background(
    facilitator_url: str,
    payment_payload: dict,
    payment_requirements: dict,
    result_sink: dict,
) -> None:
    result = await facilitator_settle(
        facilitator_url, payment_payload, payment_requirements
    )
    result_sink.update(result)
    _logger.debug(
        "x402 settle result for network=%s: %s",
        payment_requirements.get("network"),
        result,
    )
