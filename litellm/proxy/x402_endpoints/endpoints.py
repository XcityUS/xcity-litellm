"""
x402 Transaction Management — ``/x402/transactions`` and ``/x402/stats``.

Records and exposes every x402 payment received by the proxy as a Seller.
Each transaction links to a LiteLLM_SpendLogs row via request_id so that
the on-chain payment and the LLM cost are co-traceable.

Endpoints:
  POST   /x402/transactions            — record a new payment (called by middleware)
  GET    /x402/transactions            — list with pagination + filters
  GET    /x402/transactions/{tx_id}   — fetch single transaction
  PATCH  /x402/transactions/{tx_id}   — update status / tx_hash after settlement
  POST   /x402/transactions/{tx_id}/retry — trigger a re-settle for failed tx
  GET    /x402/stats                   — daily/weekly/monthly summary + top models
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from litellm._logging import verbose_proxy_logger
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.types.x402 import (
    X402PeriodStats,
    X402StatsResponse,
    X402TopModel,
    X402Transaction,
    X402TransactionCreate,
    X402TransactionListResponse,
    X402TransactionUpdate,
)

if TYPE_CHECKING:
    from litellm.proxy.proxy_server import PrismaClient
else:
    PrismaClient = Any

router = APIRouter()

_TABLE = "litellm_x402transactions"


def _require_admin(uak: UserAPIKeyAuth) -> None:
    if uak.user_role not in (
        LitellmUserRoles.PROXY_ADMIN,
        LitellmUserRoles.PROXY_ADMIN.value,
    ):
        raise HTTPException(status_code=403, detail="Proxy admin access required.")


def _row_to_tx(row) -> X402Transaction:
    data = row.model_dump() if hasattr(row, "model_dump") else dict(row)
    amount_usd = data.get("amount_usd")
    if isinstance(amount_usd, Decimal):
        amount_usd = float(amount_usd)
    amount = data.get("amount")
    if amount is not None:
        amount = int(amount)
    return X402Transaction(
        tx_id=data["tx_id"],
        request_id=data.get("request_id"),
        caller_address=data["caller_address"],
        model=data["model"],
        chain=data["chain"],
        token=data["token"],
        amount=amount or 0,
        amount_usd=amount_usd or 0.0,
        tx_hash=data.get("tx_hash"),
        status=data["status"],
        facilitator_url=data.get("facilitator_url"),
        error_message=data.get("error_message"),
        retry_count=data.get("retry_count", 0),
        settle_attempted_at=data.get("settle_attempted_at"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
    )


@router.post(
    "/x402/transactions",
    tags=["x402 Payments"],
    dependencies=[Depends(user_api_key_auth)],
    response_model=X402Transaction,
    summary="Record a new x402 payment",
)
async def record_x402_transaction(
    body: X402TransactionCreate,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> X402Transaction:
    """
    Called by the x402 payment middleware after a payment is verified.
    Admin key required.
    """
    _require_admin(user_api_key_dict)

    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not connected.",
        )

    row = await prisma_client.db.litellm_x402transactions.create(
        data={
            "tx_id": str(uuid.uuid4()),
            "request_id": body.request_id,
            "caller_address": body.caller_address,
            "model": body.model,
            "chain": body.chain,
            "token": body.token,
            "amount": body.amount,
            "amount_usd": Decimal(str(body.amount_usd)),
            "tx_hash": body.tx_hash,
            "status": body.status,
            "facilitator_url": body.facilitator_url,
        }
    )
    return _row_to_tx(row)


@router.get(
    "/x402/transactions",
    tags=["x402 Payments"],
    dependencies=[Depends(user_api_key_auth)],
    response_model=X402TransactionListResponse,
    summary="List x402 transactions",
)
async def list_x402_transactions(
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    status_filter: Optional[Literal["pending", "verified", "settled", "failed"]] = Query(
        default=None, alias="status"
    ),
    model: Optional[str] = Query(default=None),
    chain: Optional[str] = Query(default=None),
    caller_address: Optional[str] = Query(default=None),
    start_date: Optional[datetime] = Query(default=None),
    end_date: Optional[datetime] = Query(default=None),
    min_amount_usd: Optional[float] = Query(default=None),
    max_amount_usd: Optional[float] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> X402TransactionListResponse:
    """
    List x402 transactions with optional filters and pagination.
    Admin key required.
    """
    _require_admin(user_api_key_dict)

    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not connected.",
        )

    where: dict = {}
    if status_filter is not None:
        where["status"] = status_filter
    if model is not None:
        where["model"] = model
    if chain is not None:
        where["chain"] = chain
    if caller_address is not None:
        where["caller_address"] = caller_address

    created_at_filter: dict = {}
    if start_date is not None:
        created_at_filter["gte"] = start_date
    if end_date is not None:
        created_at_filter["lte"] = end_date
    if created_at_filter:
        where["created_at"] = created_at_filter

    amount_usd_filter: dict = {}
    if min_amount_usd is not None:
        amount_usd_filter["gte"] = Decimal(str(min_amount_usd))
    if max_amount_usd is not None:
        amount_usd_filter["lte"] = Decimal(str(max_amount_usd))
    if amount_usd_filter:
        where["amount_usd"] = amount_usd_filter

    rows = await prisma_client.db.litellm_x402transactions.find_many(
        where=where,
        order={"created_at": "desc"},
        take=limit,
        skip=offset,
    )
    total = await prisma_client.db.litellm_x402transactions.count(where=where)

    return X402TransactionListResponse(
        transactions=[_row_to_tx(r) for r in rows],
        total=total,
        has_more=(offset + limit) < total,
    )


@router.get(
    "/x402/transactions/{tx_id}",
    tags=["x402 Payments"],
    dependencies=[Depends(user_api_key_auth)],
    response_model=X402Transaction,
    summary="Get a single x402 transaction",
)
async def get_x402_transaction(
    tx_id: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> X402Transaction:
    _require_admin(user_api_key_dict)

    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not connected.",
        )

    row = await prisma_client.db.litellm_x402transactions.find_unique(
        where={"tx_id": tx_id}
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Transaction {tx_id!r} not found.")
    return _row_to_tx(row)


@router.patch(
    "/x402/transactions/{tx_id}",
    tags=["x402 Payments"],
    dependencies=[Depends(user_api_key_auth)],
    response_model=X402Transaction,
    summary="Update x402 transaction status",
)
async def update_x402_transaction(
    tx_id: str,
    body: X402TransactionUpdate,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> X402Transaction:
    """Update tx_hash, status, and/or error_message after a settle attempt."""
    _require_admin(user_api_key_dict)

    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not connected.",
        )

    update_data: dict = {}
    if body.tx_hash is not None:
        update_data["tx_hash"] = body.tx_hash
    if body.status is not None:
        update_data["status"] = body.status
    if body.error_message is not None:
        update_data["error_message"] = body.error_message
    if body.settle_attempted_at is not None:
        update_data["settle_attempted_at"] = body.settle_attempted_at

    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update.")

    try:
        row = await prisma_client.db.litellm_x402transactions.update(
            where={"tx_id": tx_id},
            data=update_data,
        )
    except Exception as e:
        if "RecordNotFound" in type(e).__name__ or "not found" in str(e).lower():
            raise HTTPException(
                status_code=404, detail=f"Transaction {tx_id!r} not found."
            )
        raise

    return _row_to_tx(row)


@router.post(
    "/x402/transactions/{tx_id}/retry",
    tags=["x402 Payments"],
    dependencies=[Depends(user_api_key_auth)],
    response_model=X402Transaction,
    summary="Retry settlement for a failed x402 transaction",
)
async def retry_x402_transaction(
    tx_id: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> X402Transaction:
    """
    Mark a failed transaction as pending and increment retry_count.
    The actual re-settle must be triggered by the caller (the settlement
    worker or an operator script) — this endpoint only resets the status
    so the next settle cycle picks it up.
    """
    _require_admin(user_api_key_dict)

    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not connected.",
        )

    existing = await prisma_client.db.litellm_x402transactions.find_unique(
        where={"tx_id": tx_id}
    )
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Transaction {tx_id!r} not found.")

    existing_data = (
        existing.model_dump() if hasattr(existing, "model_dump") else dict(existing)
    )
    if existing_data.get("status") != "failed":
        raise HTTPException(
            status_code=400,
            detail=f"Only 'failed' transactions can be retried. Current status: {existing_data.get('status')!r}.",
        )

    row = await prisma_client.db.litellm_x402transactions.update(
        where={"tx_id": tx_id},
        data={
            "status": "pending",
            "error_message": None,
            "retry_count": {"increment": 1},
            "settle_attempted_at": datetime.now(tz=timezone.utc),
        },
    )
    verbose_proxy_logger.info("x402 retry queued: tx_id=%s", tx_id)
    return _row_to_tx(row)


@router.get(
    "/x402/stats",
    tags=["x402 Payments"],
    dependencies=[Depends(user_api_key_auth)],
    response_model=X402StatsResponse,
    summary="x402 revenue and transaction statistics",
)
async def get_x402_stats(
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    top_models_limit: int = Query(default=10, ge=1, le=50),
) -> X402StatsResponse:
    """
    Returns daily / weekly / monthly transaction counts and revenue plus the
    top-N models by transaction volume. Admin key required.
    """
    _require_admin(user_api_key_dict)

    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not connected.",
        )

    now = datetime.now(tz=timezone.utc)
    day_start = now - timedelta(days=1)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)

    async def _period_stats(since: datetime, label: str) -> X402PeriodStats:
        rows = await prisma_client.db.litellm_x402transactions.find_many(
            where={"created_at": {"gte": since}},
            order={"created_at": "desc"},
        )
        total_usd = sum(
            float(r.amount_usd) if isinstance(r.amount_usd, Decimal) else float(r.amount_usd or 0)
            for r in rows
        )
        count = len(rows)
        settled = sum(1 for r in rows if (r.model_dump() if hasattr(r, "model_dump") else dict(r)).get("status") == "settled")
        failed = sum(1 for r in rows if (r.model_dump() if hasattr(r, "model_dump") else dict(r)).get("status") == "failed")
        return X402PeriodStats(
            period=label,
            transaction_count=count,
            total_amount_usd=round(total_usd, 6),
            avg_amount_usd=round(total_usd / count, 6) if count else 0.0,
            settled_count=settled,
            failed_count=failed,
        )

    daily, weekly, monthly = (
        await _period_stats(day_start, "day"),
        await _period_stats(week_start, "week"),
        await _period_stats(month_start, "month"),
    )

    pending_count = await prisma_client.db.litellm_x402transactions.count(
        where={"status": {"in": ["pending", "verified"]}}
    )

    # Top models by transaction count (last 30 days)
    month_rows = await prisma_client.db.litellm_x402transactions.find_many(
        where={"created_at": {"gte": month_start}},
        order={"created_at": "desc"},
    )
    model_totals: dict = {}
    for r in month_rows:
        data = r.model_dump() if hasattr(r, "model_dump") else dict(r)
        m = data.get("model", "")
        amt = float(data.get("amount_usd") or 0)
        if m not in model_totals:
            model_totals[m] = {"count": 0, "usd": 0.0}
        model_totals[m]["count"] += 1
        model_totals[m]["usd"] += amt

    top_models: List[X402TopModel] = sorted(
        [
            X402TopModel(
                model=m,
                transaction_count=v["count"],
                total_amount_usd=round(v["usd"], 6),
            )
            for m, v in model_totals.items()
        ],
        key=lambda x: x.transaction_count,
        reverse=True,
    )[:top_models_limit]

    return X402StatsResponse(
        daily=daily,
        weekly=weekly,
        monthly=monthly,
        top_models=top_models,
        pending_settlement_count=pending_count,
    )
