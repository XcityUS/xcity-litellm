"""Pydantic types for x402 Payment Protocol transaction tracking."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

X402TransactionStatus = Literal["pending", "verified", "settled", "failed"]


class X402TransactionCreate(BaseModel):
    """Payload posted by the x402 middleware when a payment is verified."""

    request_id: Optional[str] = None
    caller_address: str = Field(..., description="Payer wallet address")
    model: str
    chain: str = Field(..., description="e.g. 'base', 'ethereum'")
    token: str = Field(..., description="e.g. 'USDC'")
    amount: int = Field(..., description="Smallest token unit (USDC: 1000000 = $1.00)")
    amount_usd: float
    tx_hash: Optional[str] = None
    status: X402TransactionStatus = "pending"
    facilitator_url: Optional[str] = None


class X402TransactionUpdate(BaseModel):
    """Patch payload for updating transaction status after settlement."""

    tx_hash: Optional[str] = None
    status: Optional[X402TransactionStatus] = None
    error_message: Optional[str] = None
    settle_attempted_at: Optional[datetime] = None


class X402Transaction(BaseModel):
    """Full transaction row returned by read endpoints."""

    tx_id: str
    request_id: Optional[str] = None
    caller_address: str
    model: str
    chain: str
    token: str
    amount: int
    amount_usd: float
    tx_hash: Optional[str] = None
    status: str
    facilitator_url: Optional[str] = None
    error_message: Optional[str] = None
    retry_count: int = 0
    settle_attempted_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class X402TransactionListResponse(BaseModel):
    transactions: List[X402Transaction]
    total: int
    has_more: bool


class X402PeriodStats(BaseModel):
    """Statistics for a single time window."""

    period: str  # e.g. "day", "week", "month"
    transaction_count: int
    total_amount_usd: float
    avg_amount_usd: float
    settled_count: int
    failed_count: int


class X402TopModel(BaseModel):
    model: str
    transaction_count: int
    total_amount_usd: float


class X402StatsResponse(BaseModel):
    daily: X402PeriodStats
    weekly: X402PeriodStats
    monthly: X402PeriodStats
    top_models: List[X402TopModel]
    pending_settlement_count: int
