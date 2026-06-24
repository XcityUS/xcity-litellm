"""Pydantic types for the x402 payment protocol (multichain)."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# CAIP-2 network identifiers supported by XCity
# ---------------------------------------------------------------------------

NETWORK_BASE = "eip155:8453"
NETWORK_ARBITRUM = "eip155:42161"
NETWORK_SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

# USDC contract / mint addresses per network
USDC_CONTRACT_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_CONTRACT_ARBITRUM = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
USDC_MINT_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

EVM_NETWORKS = {NETWORK_BASE, NETWORK_ARBITRUM}
SOLANA_NETWORKS = {NETWORK_SOLANA}


class X402PaymentAccept(BaseModel):
    """One entry in the 402 ``accepts[]`` array (per the x402 spec)."""

    scheme: str = "exact"
    network: str = Field(..., description="CAIP-2 network ID")
    maxAmountRequired: str = Field(
        ..., description="Token amount in smallest unit (USDC: 6 decimals)"
    )
    resource: str = Field(..., description="URL of the gated resource")
    description: str = ""
    mimeType: str = "application/json"
    payTo: str = Field(..., description="Recipient address or SPL token account")
    maxTimeoutSeconds: int = 300
    asset: str = Field(
        ..., description="Contract address (EVM) or mint address (Solana)"
    )
    extra: Dict[str, Any] = Field(default_factory=dict)


class X402Response(BaseModel):
    """Body of an HTTP 402 PaymentRequired response."""

    x402Version: int = 1
    accepts: List[X402PaymentAccept]
    error: str = "Payment required"


class X402EVMAuthorization(BaseModel):
    """EIP-3009 transferWithAuthorization parameters."""

    from_: str = Field(..., alias="from")
    to: str
    value: str
    validAfter: str = "0"
    validBefore: str
    nonce: str

    model_config = {"populate_by_name": True}


class X402SolanaAuthorization(BaseModel):
    """Solana SPL token transfer parameters."""

    from_: str = Field(..., alias="from", description="Base58 sender public key")
    to: str = Field(..., description="Base58 recipient SPL token account")
    value: str = Field(..., description="Token amount in smallest unit")
    nonce: str = Field(..., description="Base58 nonce")

    model_config = {"populate_by_name": True}


class X402PaymentPayload(BaseModel):
    """
    Decoded content of the ``X-PAYMENT`` request header.

    Clients base64-encode this JSON and set it as the ``X-PAYMENT`` header.
    """

    x402Version: int = 1
    scheme: str = "exact"
    network: str = Field(..., description="CAIP-2 network ID")
    payload: Dict[str, Any] = Field(
        ...,
        description="Chain-specific payload: {signature, authorization}",
    )


class X402SettlementReceipt(BaseModel):
    """
    Content of the ``X-PAYMENT-RESPONSE`` header returned to the client
    after a successful payment-gated request.
    """

    success: bool
    network: str
    txHash: Optional[str] = None
    payer: Optional[str] = None
    settledAt: Optional[str] = None
    error: Optional[str] = None
