"""x402 payment protocol support for the LiteLLM gateway.

Implements the Seller side of the x402 HTTP payment standard:
  - Pricing engine: converts litellm model USD/token pricing to USDC base units
  - Middleware: returns HTTP 402 with PaymentRequired JSON for unauthenticated requests

Enable with ``X402_ENABLED=true`` and ``X402_PAY_TO=0x<address>``.
See ``config.py`` for the full list of env vars.
"""

from litellm.proxy.x402.config import X402Config
from litellm.proxy.x402.middleware import X402Middleware
from litellm.proxy.x402.pricing import UsdcPricingEngine

__all__ = ["X402Config", "X402Middleware", "UsdcPricingEngine"]
