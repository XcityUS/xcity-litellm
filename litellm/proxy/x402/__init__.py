"""
x402 Payment Protocol integration for LiteLLM Proxy.

Provides HTTP 402 Payment Required support via the Coinbase CDP Facilitator API,
with an extensible base for other facilitators (e.g. Stripe x402).

Usage in proxy config:
    x402:
      enabled: true
      facilitator: coinbase_cdp
      facilitator_url: https://api.developer.coinbase.com/rpc/v1/base  # optional
      api_key: os.environ/CDP_API_KEY
      pay_to: "0x..."           # wallet address to receive USDC
      amount: "1000000"         # USDC base units (1000000 = $1.00)
      asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base
      network: base
      description: "Pay to access LiteLLM API"
      payment_header: X-PAYMENT    # header to read; PAYMENT-SIGNATURE also accepted
      protected_routes:            # if omitted, all /v1/* routes are protected
        - /v1/chat/completions
        - /v1/completions
"""

from litellm.proxy.x402.facilitator import (
    BaseFacilitatorClient,
    CDPFacilitatorClient,
    SettleResult,
    VerifyResult,
)
from litellm.proxy.x402.middleware import X402Config, X402PaymentMiddleware

__all__ = [
    "BaseFacilitatorClient",
    "CDPFacilitatorClient",
    "SettleResult",
    "VerifyResult",
    "X402Config",
    "X402PaymentMiddleware",
]
