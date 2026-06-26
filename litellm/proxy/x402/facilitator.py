"""
Facilitator clients for x402 payment verification and settlement.

Provides a base class (BaseFacilitatorClient) that future facilitators
(e.g. Stripe x402) can extend, plus the concrete CDP implementation.
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# CDP facilitator endpoints for the Base network
CDP_DEFAULT_BASE_URL = "https://api.developer.coinbase.com/rpc/v1/base"

_MAX_SETTLE_RETRIES = 3
_BASE_RETRY_DELAY_SECONDS = 1.0


@dataclass
class VerifyResult:
    is_valid: bool
    invalid_reason: Optional[str]
    payer: Optional[str]


@dataclass
class SettleResult:
    success: bool
    tx_hash: Optional[str]
    network_id: Optional[str]
    error: Optional[str] = field(default=None)


class BaseFacilitatorClient(ABC):
    """
    Abstract facilitator: any x402 backend (CDP, Stripe, etc.) must implement
    verify() and settle().  The retry logic is provided by this base class so
    concrete subclasses only need to worry about the single-attempt HTTP calls.
    """

    @abstractmethod
    async def verify(
        self, payment: str, payment_requirements: Dict[str, Any]
    ) -> VerifyResult:
        """Verify that a payment header is valid against the given requirements."""

    @abstractmethod
    async def settle(
        self, payment: str, payment_requirements: Dict[str, Any]
    ) -> SettleResult:
        """Attempt a single on-chain settlement. Returns a SettleResult."""

    async def settle_with_retry(
        self, payment: str, payment_requirements: Dict[str, Any]
    ) -> SettleResult:
        """
        Settle with exponential back-off, up to _MAX_SETTLE_RETRIES attempts.

        Delays: 1 s, 2 s (max 3 attempts total).
        """
        last_result: Optional[SettleResult] = None
        for attempt in range(_MAX_SETTLE_RETRIES):
            result = await self.settle(payment, payment_requirements)
            last_result = result
            if result.success:
                return result
            if attempt < _MAX_SETTLE_RETRIES - 1:
                delay = _BASE_RETRY_DELAY_SECONDS * (2**attempt)
                logger.warning(
                    "x402 settle attempt %d/%d failed (tx_hash=%s error=%s), "
                    "retrying in %.1fs",
                    attempt + 1,
                    _MAX_SETTLE_RETRIES,
                    result.tx_hash,
                    result.error,
                    delay,
                )
                await asyncio.sleep(delay)

        logger.error(
            "x402 settle failed after %d attempts: %s",
            _MAX_SETTLE_RETRIES,
            last_result.error if last_result else "unknown",
        )
        return last_result or SettleResult(
            success=False,
            tx_hash=None,
            network_id=None,
            error="max retries exceeded",
        )


class CDPFacilitatorClient(BaseFacilitatorClient):
    """
    Coinbase CDP Facilitator client.

    Calls:
      POST <base_url>/verify  — validate a payment header
      POST <base_url>/settle  — trigger on-chain USDC settlement

    Pricing: free for the first 1 000 transactions/month; $0.001/tx thereafter.
    Base chain settlement latency is ~2 s, so settle() is always called from a
    background task via settle_with_retry() to avoid blocking the HTTP response.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = (base_url or CDP_DEFAULT_BASE_URL).rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    # ------------------------------------------------------------------
    # BaseFacilitatorClient implementation
    # ------------------------------------------------------------------

    async def verify(
        self, payment: str, payment_requirements: Dict[str, Any]
    ) -> VerifyResult:
        url = f"{self._base_url}/verify"
        payload = {
            "payment": payment,
            "paymentRequirements": payment_requirements,
        }
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
            elapsed_ms = (time.monotonic() - t0) * 1000
            data: Dict[str, Any] = resp.json()
            logger.info(
                "x402 verify: http_status=%d is_valid=%s payer=%s elapsed_ms=%.1f",
                resp.status_code,
                data.get("isValid"),
                data.get("payer"),
                elapsed_ms,
            )
            resp.raise_for_status()
            return VerifyResult(
                is_valid=bool(data.get("isValid")),
                invalid_reason=data.get("invalidReason"),
                payer=data.get("payer"),
            )
        except httpx.HTTPStatusError as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.error(
                "x402 verify HTTP error: status=%d elapsed_ms=%.1f body=%s",
                exc.response.status_code,
                elapsed_ms,
                exc.response.text[:200],
            )
            raise
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.error("x402 verify error: %s elapsed_ms=%.1f", exc, elapsed_ms)
            raise

    async def settle(
        self, payment: str, payment_requirements: Dict[str, Any]
    ) -> SettleResult:
        url = f"{self._base_url}/settle"
        payload = {
            "payment": payment,
            "paymentRequirements": payment_requirements,
        }
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
            elapsed_ms = (time.monotonic() - t0) * 1000
            data = resp.json()
            logger.info(
                "x402 settle: http_status=%d success=%s tx_hash=%s network=%s elapsed_ms=%.1f",
                resp.status_code,
                data.get("success"),
                data.get("txHash"),
                data.get("networkId"),
                elapsed_ms,
            )
            resp.raise_for_status()
            return SettleResult(
                success=bool(data.get("success")),
                tx_hash=data.get("txHash"),
                network_id=data.get("networkId"),
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.error("x402 settle error: %s elapsed_ms=%.1f", exc, elapsed_ms)
            return SettleResult(
                success=False,
                tx_hash=None,
                network_id=None,
                error=str(exc),
            )


def build_facilitator(
    facilitator_type: str,
    base_url: Optional[str],
    api_key: Optional[str],
) -> BaseFacilitatorClient:
    """
    Factory that resolves a facilitator type name to a client instance.

    Supported values for facilitator_type:
      "coinbase_cdp"  (default) — Coinbase Developer Platform facilitator

    New facilitators can be added by registering them here.
    """
    if facilitator_type in ("coinbase_cdp", "cdp", "coinbase"):
        return CDPFacilitatorClient(base_url=base_url, api_key=api_key)
    raise ValueError(
        f"Unknown x402 facilitator type: {facilitator_type!r}. "
        "Supported: 'coinbase_cdp'"
    )
