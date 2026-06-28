"""x402 payment-aware HTTP client.

Wraps ``httpx.AsyncClient`` and transparently handles the x402 micropayment
protocol (https://x402.org).  When a server returns HTTP 402, the client:

  1. Parses the ``PaymentRequired`` JSON body to extract payment requirements.
  2. Calls the user-supplied ``signer`` coroutine to produce a signed
     ``X-Payment`` header payload.
  3. Retries the original request with the ``X-Payment`` header attached.
  4. Returns the final response (200 or next 402/error) along with the
     settlement receipt extracted from the ``X-PAYMENT-RESPONSE`` header.

The client is intentionally wallet-agnostic: it does NOT import any Web3 /
EIP-3009 logic itself.  The ``signer`` callable is the integration point —
pass a Coinbase CDP signer, an EthAccount signer, or a test stub.

Usage (async)::

    from gateway.x402_client import X402Client, PaymentRequirements

    async def my_signer(reqs: PaymentRequirements) -> dict:
        # Build and return the EIP-3009 authorization payload dict
        return { ... }

    async with X402Client(signer=my_signer) as client:
        response, receipt = await client.request_with_payment(
            "POST",
            "https://api.xcity.us/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [...]},
        )

Mock signer for tests::

    from gateway.x402_client import mock_signer
    # Returns a deterministic payment payload without a real wallet.
"""

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

import httpx

logger = logging.getLogger(__name__)

_PAYMENT_HEADER = "x-payment"
_PAYMENT_RESPONSE_HEADER = "x-payment-response"
_X402_VERSION = 1


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PaymentRequirements:
    """Typed view of one entry in ``accepts[]`` from a 402 response."""

    scheme: str
    network: str
    max_amount_required: str
    resource: str
    pay_to: str
    asset: str
    max_timeout_seconds: int = 300
    description: str = ""
    mime_type: str = "application/json"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "PaymentRequirements":
        return cls(
            scheme=d["scheme"],
            network=d["network"],
            max_amount_required=d["maxAmountRequired"],
            resource=d["resource"],
            pay_to=d["payTo"],
            asset=d["asset"],
            max_timeout_seconds=d.get("maxTimeoutSeconds", 300),
            description=d.get("description", ""),
            mime_type=d.get("mimeType", "application/json"),
            extra=d.get("extra", {}),
        )


@dataclass
class PaymentResponse:
    """Settlement receipt returned in the ``X-PAYMENT-RESPONSE`` header."""

    is_valid: bool
    payer: Optional[str] = None
    tx_hash: Optional[str] = None
    invalid_reason: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_header(cls, header_value: str) -> "PaymentResponse":
        try:
            raw = json.loads(base64.b64decode(header_value))
        except Exception:
            raw = {}
        return cls(
            is_valid=raw.get("isValid", False),
            payer=raw.get("payer"),
            tx_hash=raw.get("txHash"),
            invalid_reason=raw.get("invalidReason"),
            raw=raw,
        )


# ---------------------------------------------------------------------------
# Signer type alias
# ---------------------------------------------------------------------------

SignerFn = Callable[[PaymentRequirements], Coroutine[Any, Any, dict]]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class X402Client:
    """Async HTTP client that automatically handles x402 payment challenges.

    Parameters
    ----------
    signer:
        Async callable that receives ``PaymentRequirements`` and returns a
        signed payment payload dict (the value for the ``X-Payment`` header).
    max_retries:
        How many times to retry after a 402.  Usually 1 is enough; set higher
        if you expect facilitator transient errors.
    httpx_kwargs:
        Extra keyword arguments forwarded to ``httpx.AsyncClient``.
    """

    def __init__(
        self,
        signer: SignerFn,
        max_retries: int = 1,
        **httpx_kwargs: Any,
    ) -> None:
        self._signer = signer
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(**httpx_kwargs)

    async def __aenter__(self) -> "X402Client":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.__aexit__(*args)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def request_with_payment(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response, Optional[PaymentResponse]]:
        """Send a request; handle 402 by paying and retrying.

        Returns
        -------
        (response, receipt)
            *response* is the final HTTP response (200 on success, 402 if
            payment was rejected).  *receipt* is the settlement confirmation
            extracted from the ``X-PAYMENT-RESPONSE`` header, or ``None`` if
            the final response has no such header.
        """
        for attempt in range(self._max_retries + 1):
            resp = await self._client.request(method, url, **kwargs)

            if resp.status_code != 402:
                receipt = self._extract_receipt(resp)
                return resp, receipt

            logger.debug("Got 402 on attempt %d/%d — initiating payment", attempt + 1, self._max_retries + 1)

            try:
                body = resp.json()
            except Exception:
                logger.warning("402 response has non-JSON body; cannot pay")
                return resp, None

            accepts = body.get("accepts", [])
            if not accepts:
                logger.warning("402 body has no 'accepts' entries")
                return resp, None

            reqs = PaymentRequirements.from_dict(accepts[0])

            try:
                payment_payload = await self._signer(reqs)
            except Exception as exc:
                logger.error("Signer raised: %s", exc)
                return resp, None

            payment_header = _encode_payment_header(payment_payload)
            kwargs = {**kwargs, "headers": {**kwargs.get("headers", {}), _PAYMENT_HEADER: payment_header}}

        return resp, None

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    async def post(
        self, url: str, **kwargs: Any
    ) -> tuple[httpx.Response, Optional[PaymentResponse]]:
        return await self.request_with_payment("POST", url, **kwargs)

    async def get(
        self, url: str, **kwargs: Any
    ) -> tuple[httpx.Response, Optional[PaymentResponse]]:
        return await self.request_with_payment("GET", url, **kwargs)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_receipt(self, resp: httpx.Response) -> Optional[PaymentResponse]:
        header_val = resp.headers.get(_PAYMENT_RESPONSE_HEADER)
        if not header_val:
            return None
        return PaymentResponse.from_header(header_val)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _encode_payment_header(payload: dict) -> str:
    """Base64-encode a payment payload dict for use as the X-Payment header."""
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


# ---------------------------------------------------------------------------
# Test / mock signer
# ---------------------------------------------------------------------------


async def mock_signer(reqs: PaymentRequirements, signature: str = "0xmocksig") -> dict:
    """Deterministic signer for tests — does NOT produce a real EIP-3009 authorization.

    Returns a valid x402 payload structure with a fake signature so the
    mock facilitator in tests can verify the payload shape without a real wallet.
    """
    return {
        "x402Version": _X402_VERSION,
        "scheme": reqs.scheme,
        "network": reqs.network,
        "payload": {
            "signature": signature,
            "authorization": {
                "from": "0xTestWallet0000000000000000000000000001",
                "to": reqs.pay_to,
                "value": reqs.max_amount_required,
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0xtest_nonce",
            },
        },
    }
