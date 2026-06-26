"""USDC pricing engine for the x402 middleware.

Converts litellm model pricing (USD/token from ``model_prices_and_context_window.json``)
to USDC base units (6 decimal places, i.e. 1 USDC == 1_000_000).

Pricing formula
---------------
::

    usdc_base_units = ceil(
        (input_tokens * input_cost_per_token
         + output_tokens * output_cost_per_token)
        * markup_factor
        / usd_usdc_rate
        * USDC_SCALE
    )

Configuration
-------------
- Per-model env override: ``X402_PRICE_<MODEL_SLUG>`` (in USDC base units).
  The slug is the model name uppercased with non-alphanumeric chars replaced by ``_``.
  Example: ``X402_PRICE_GPT_4O=50000`` pins gpt-4o to 0.05 USDC per request.
- ``X402_MARKUP_FACTOR`` — float multiplier on top of base cost (default ``1.0``).
- ``X402_USD_USDC_RATE`` — USD per USDC (default ``1.0``, 1:1 parity).
- ``X402_ESTIMATED_INPUT_TOKENS`` / ``X402_ESTIMATED_OUTPUT_TOKENS`` — token counts
  used when a caller does not supply explicit counts (defaults ``1000`` / ``200``).
"""

import math
import os
import re
from typing import Optional

import litellm

from litellm.proxy.x402.config import USDC_SCALE, X402Config

_ENV_SLUG_RE = re.compile(r"[^A-Z0-9]")


def _model_to_env_slug(model: str) -> str:
    """Upper-snake slug suitable for an environment variable suffix."""
    return _ENV_SLUG_RE.sub("_", model.upper())


class UsdcPricingEngine:
    """Converts litellm model pricing to USDC base units for x402 payment requirements.

    Lookup order for each model
    ---------------------------
    1. ``X402_PRICE_<MODEL_SLUG>`` env var (already in USDC base units — returned as-is).
    2. ``litellm.model_cost`` table entry (input + output cost per token).
       Falls back to the bare model name (without provider prefix) on a second lookup.
    3. Hard default: 100_000 base units (0.10 USDC) when no pricing data is found.
    """

    _DEFAULT_FALLBACK_BASE_UNITS: int = 100_000  # 0.10 USDC

    def __init__(self, config: X402Config) -> None:
        self._config = config

    def compute_usdc_base_units(
        self,
        model: str,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> int:
        """Return USDC amount in base units (integer, 6 decimals) for one request.

        Parameters
        ----------
        model:
            litellm model identifier (e.g. ``"gpt-4o"``, ``"deepseek/deepseek-chat"``).
        input_tokens:
            Estimated input token count.  Defaults to ``config.estimated_input_tokens``.
        output_tokens:
            Estimated output token count.  Defaults to ``config.estimated_output_tokens``.
        """
        in_tok = input_tokens if input_tokens is not None else self._config.estimated_input_tokens
        out_tok = output_tokens if output_tokens is not None else self._config.estimated_output_tokens

        # 1. Per-model env override (already in base units)
        slug = _model_to_env_slug(model)
        env_override = os.getenv(f"X402_PRICE_{slug}")
        if env_override is not None:
            return int(env_override)

        # 2. litellm model cost table
        model_info = litellm.model_cost.get(model)
        if model_info is None:
            # Try without provider prefix: "provider/model-name" → "model-name"
            bare = model.split("/")[-1]
            model_info = litellm.model_cost.get(bare)

        if model_info is None:
            return self._DEFAULT_FALLBACK_BASE_UNITS

        input_cost: float = model_info.get("input_cost_per_token") or 0.0
        output_cost: float = model_info.get("output_cost_per_token") or 0.0

        usd_cost = in_tok * input_cost + out_tok * output_cost
        usd_with_markup = usd_cost * self._config.markup_factor
        usdc_float = usd_with_markup / self._config.usd_usdc_rate * USDC_SCALE

        return max(1, math.ceil(usdc_float))

    def describe(self, model: str, usdc_base_units: int) -> str:
        """Human-readable description embedded in the 402 payment requirement."""
        usdc_display = usdc_base_units / USDC_SCALE
        return (
            f"{model} via USDC — "
            f"est. {self._config.estimated_input_tokens} input + "
            f"{self._config.estimated_output_tokens} output tokens "
            f"(${usdc_display:.6f} USDC)"
        )
