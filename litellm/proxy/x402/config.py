"""Configuration for the x402 payment middleware.

All settings are loaded from environment variables so pricing and routing
can be changed without redeployment.
"""

import os
from dataclasses import dataclass, field
from typing import List

USDC_DECIMALS: int = 6
USDC_SCALE: int = 10**USDC_DECIMALS  # 1 USDC == 1_000_000 base units

BASE_MAINNET_NETWORK: str = "eip155:8453"
BASE_USDC_ADDRESS: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


@dataclass
class X402Config:
    """Runtime configuration for the x402 middleware and pricing engine.

    Attributes
    ----------
    enabled:
        Master switch.  When False the middleware is a no-op.
    pay_to:
        EVM address that receives USDC payments (required when enabled).
    network:
        CAIP-2 network identifier (default: Base mainnet).
    asset:
        ERC-20 contract address for the payment token (default: USDC on Base).
    routes:
        List of URL paths that require x402 payment.
    markup_factor:
        Multiplier applied on top of the base model cost (e.g. 1.2 = 20% markup).
    usd_usdc_rate:
        How many USD per 1 USDC (default 1.0 for 1:1 parity).
        Reserve this interface for future exchange-rate integration.
    estimated_input_tokens:
        Default estimated input tokens used when computing the upfront price.
    estimated_output_tokens:
        Default estimated output tokens used when computing the upfront price.
    max_timeout_seconds:
        Seconds until the payment requirement expires.
    """

    enabled: bool
    pay_to: str
    network: str
    asset: str
    routes: List[str]
    markup_factor: float
    usd_usdc_rate: float
    estimated_input_tokens: int
    estimated_output_tokens: int
    max_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "X402Config":
        """Build config from environment variables."""
        enabled = os.getenv("X402_ENABLED", "false").lower() == "true"
        pay_to = os.getenv("X402_PAY_TO", "")
        network = os.getenv("X402_NETWORK", BASE_MAINNET_NETWORK)
        asset = os.getenv("X402_ASSET", BASE_USDC_ADDRESS)
        routes_raw = os.getenv("X402_ROUTES", "/v1/chat/completions")
        routes = [r.strip() for r in routes_raw.split(",") if r.strip()]
        markup_factor = float(os.getenv("X402_MARKUP_FACTOR", "1.0"))
        usd_usdc_rate = float(os.getenv("X402_USD_USDC_RATE", "1.0"))
        estimated_input_tokens = int(os.getenv("X402_ESTIMATED_INPUT_TOKENS", "1000"))
        estimated_output_tokens = int(os.getenv("X402_ESTIMATED_OUTPUT_TOKENS", "200"))
        max_timeout_seconds = int(os.getenv("X402_MAX_TIMEOUT_SECONDS", "300"))
        return cls(
            enabled=enabled,
            pay_to=pay_to,
            network=network,
            asset=asset,
            routes=routes,
            markup_factor=markup_factor,
            usd_usdc_rate=usd_usdc_rate,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
            max_timeout_seconds=max_timeout_seconds,
        )
