"""FastAPI router for the /api/v1/x402/pay pay-per-request demonstration endpoint.

Provides a standalone, x402-gated route that external agents can call to
verify the end-to-end payment flow without routing through the full LLM proxy.
The endpoint is intentionally thin: it confirms payment receipt and echoes the
settlement data back so demos and integration tests can assert the complete
402 → sign → 200 + receipt cycle without needing a live LLM backend.

This router is mounted INSIDE the X402Middleware, so the middleware handles
the 402 challenge / X-Payment verification before the handler is ever reached.
The handler only runs when a valid payment has been verified.

Route
-----
POST /api/v1/x402/pay

Request body (JSON, optional)
------------------------------
{
    "model":   "gpt-4o",          // LLM model the client intends to call
    "message": "Hello, world!"    // Prompt snippet for logging
}

Response 200
------------
{
    "status":  "payment_verified",
    "model":   "gpt-4o",
    "message": "Hello, world!",
    "network": "eip155:8453",
    "token":   "USDC"
}

Add to gateway/routes/allowlist.py's GATEWAY_PATH_PREFIXES to expose on the
gateway pod::

    "/api/v1/x402/",
"""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/v1/x402", tags=["x402 Pay"])

_NETWORK = "eip155:8453"
_TOKEN = "USDC"


class PayRequest(BaseModel):
    model: Optional[str] = None
    message: Optional[str] = None


class PayResponse(BaseModel):
    status: str
    model: Optional[str]
    message: Optional[str]
    network: str
    token: str


@router.post(
    "/pay",
    response_model=PayResponse,
    summary="x402 pay-per-request verification endpoint",
    description=(
        "Confirms that an x402 payment has been verified by the gateway middleware. "
        "Returns a receipt echo so callers can validate the end-to-end flow. "
        "Requires a valid X-Payment header (handled by X402Middleware before this "
        "handler is reached)."
    ),
)
async def x402_pay(body: PayRequest) -> PayResponse:
    """Confirm payment and return a receipt summary."""
    return PayResponse(
        status="payment_verified",
        model=body.model,
        message=body.message,
        network=_NETWORK,
        token=_TOKEN,
    )
