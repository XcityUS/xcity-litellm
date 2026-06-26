"""Unit tests for x402 transaction management endpoints.

These tests are pure unit tests — they mock the Prisma client and focus on
the endpoint logic: request parsing, response shaping, pagination, filtering,
and status transitions.  No real database or blockchain is needed.

Requires the full litellm proxy-dev dependency group (installed in CI via
``uv sync --group proxy-dev``). Tests are automatically skipped when these
packages are absent.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

proxy_deps = pytest.importorskip(
    "litellm.proxy.x402_endpoints.endpoints",
    reason="litellm proxy-dev deps not installed (run: uv sync --group proxy-dev)",
)

from fastapi.testclient import TestClient as FastAPITestClient

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.x402_endpoints.endpoints import router
from litellm.types.x402 import (
    X402Transaction,
    X402TransactionCreate,
    X402TransactionListResponse,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_admin_key() -> UserAPIKeyAuth:
    key = UserAPIKeyAuth(
        api_key="sk-admin-test",
        user_role=LitellmUserRoles.PROXY_ADMIN,
    )
    return key


def _make_tx_row(
    tx_id: Optional[str] = None,
    caller_address: str = "0xCaller",
    model: str = "gpt-4o",
    chain: str = "base",
    token: str = "USDC",
    amount: int = 10000,
    amount_usd: float = 0.01,
    status: str = "settled",
    tx_hash: Optional[str] = "0xhash",
    retry_count: int = 0,
    error_message: Optional[str] = None,
) -> MagicMock:
    row = MagicMock()
    row.model_dump.return_value = {
        "tx_id": tx_id or str(uuid.uuid4()),
        "request_id": "req-001",
        "caller_address": caller_address,
        "model": model,
        "chain": chain,
        "token": token,
        "amount": amount,
        "amount_usd": Decimal(str(amount_usd)),
        "tx_hash": tx_hash,
        "status": status,
        "facilitator_url": "https://x402.org/facilitator",
        "error_message": error_message,
        "retry_count": retry_count,
        "settle_attempted_at": None,
        "created_at": datetime.now(tz=timezone.utc),
        "updated_at": datetime.now(tz=timezone.utc),
    }
    # Make direct attribute access work too
    data = row.model_dump.return_value
    for k, v in data.items():
        setattr(row, k, v)
    return row


def _make_prisma_client(rows=None, total=None):
    """Return a mock prisma_client with pre-configured x402 table methods."""
    rows = rows or []
    total = len(rows) if total is None else total

    table = MagicMock()
    table.create = AsyncMock(return_value=rows[0] if rows else _make_tx_row())
    table.find_many = AsyncMock(return_value=rows)
    table.find_unique = AsyncMock(return_value=rows[0] if rows else None)
    table.update = AsyncMock(return_value=rows[0] if rows else _make_tx_row())
    table.count = AsyncMock(return_value=total)

    prisma = MagicMock()
    prisma.db.litellm_x402transactions = table
    return prisma


# ---------------------------------------------------------------------------
# Tests — record transaction
# ---------------------------------------------------------------------------


class TestRecordTransaction:
    """POST /x402/transactions"""

    @pytest.mark.asyncio
    async def test_creates_transaction_returns_x402_transaction(self):
        tx_id = str(uuid.uuid4())
        row = _make_tx_row(tx_id=tx_id, status="pending")
        prisma = _make_prisma_client(rows=[row])
        admin = _make_admin_key()

        body = X402TransactionCreate(
            caller_address="0xCaller",
            model="gpt-4o",
            chain="base",
            token="USDC",
            amount=10000,
            amount_usd=0.01,
        )

        from litellm.proxy.x402_endpoints.endpoints import record_x402_transaction

        with patch(
            "litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma
        ):
            result = await record_x402_transaction(body, user_api_key_dict=admin)

        assert isinstance(result, X402Transaction)
        assert result.caller_address == "0xCaller"
        assert result.model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_non_admin_raises_403(self):
        from fastapi import HTTPException

        from litellm.proxy.x402_endpoints.endpoints import record_x402_transaction
        from litellm.proxy._types import LitellmUserRoles

        non_admin = UserAPIKeyAuth(
            api_key="sk-user",
            user_role=LitellmUserRoles.INTERNAL_USER,
        )
        body = X402TransactionCreate(
            caller_address="0xCaller",
            model="gpt-4o",
            chain="base",
            token="USDC",
            amount=10000,
            amount_usd=0.01,
        )
        with pytest.raises(HTTPException) as exc_info:
            await record_x402_transaction(body, user_api_key_dict=non_admin)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_503_when_database_not_connected(self):
        from fastapi import HTTPException

        from litellm.proxy.x402_endpoints.endpoints import record_x402_transaction

        admin = _make_admin_key()
        body = X402TransactionCreate(
            caller_address="0xCaller",
            model="gpt-4o",
            chain="base",
            token="USDC",
            amount=10000,
            amount_usd=0.01,
        )
        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", None):
            with pytest.raises(HTTPException) as exc_info:
                await record_x402_transaction(body, user_api_key_dict=admin)
        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Tests — list transactions
# ---------------------------------------------------------------------------


class TestListTransactions:
    """GET /x402/transactions"""

    @pytest.mark.asyncio
    async def test_returns_list_response(self):
        rows = [_make_tx_row(), _make_tx_row()]
        prisma = _make_prisma_client(rows=rows, total=2)
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import list_x402_transactions

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            result = await list_x402_transactions(
                user_api_key_dict=admin,
                status_filter=None,
                model=None,
                chain=None,
                caller_address=None,
                start_date=None,
                end_date=None,
                min_amount_usd=None,
                max_amount_usd=None,
                limit=50,
                offset=0,
            )

        assert isinstance(result, X402TransactionListResponse)
        assert result.total == 2
        assert len(result.transactions) == 2

    @pytest.mark.asyncio
    async def test_pagination_has_more_flag(self):
        rows = [_make_tx_row()]
        prisma = _make_prisma_client(rows=rows, total=100)
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import list_x402_transactions

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            result = await list_x402_transactions(
                user_api_key_dict=admin,
                status_filter=None,
                model=None,
                chain=None,
                caller_address=None,
                start_date=None,
                end_date=None,
                min_amount_usd=None,
                max_amount_usd=None,
                limit=50,
                offset=0,
            )

        assert result.has_more is True

    @pytest.mark.asyncio
    async def test_last_page_has_more_false(self):
        rows = [_make_tx_row()]
        prisma = _make_prisma_client(rows=rows, total=51)
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import list_x402_transactions

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            result = await list_x402_transactions(
                user_api_key_dict=admin,
                status_filter=None,
                model=None,
                chain=None,
                caller_address=None,
                start_date=None,
                end_date=None,
                min_amount_usd=None,
                max_amount_usd=None,
                limit=50,
                offset=50,
            )

        assert result.has_more is False

    @pytest.mark.asyncio
    async def test_status_filter_passed_to_db(self):
        prisma = _make_prisma_client(rows=[], total=0)
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import list_x402_transactions

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            await list_x402_transactions(
                user_api_key_dict=admin,
                status_filter="settled",
                model=None,
                chain=None,
                caller_address=None,
                start_date=None,
                end_date=None,
                min_amount_usd=None,
                max_amount_usd=None,
                limit=50,
                offset=0,
            )

        call_kwargs = prisma.db.litellm_x402transactions.find_many.call_args
        where = call_kwargs.kwargs.get("where") or call_kwargs.args[0]
        assert where.get("status") == "settled"


# ---------------------------------------------------------------------------
# Tests — get single transaction
# ---------------------------------------------------------------------------


class TestGetTransaction:
    """GET /x402/transactions/{tx_id}"""

    @pytest.mark.asyncio
    async def test_returns_transaction(self):
        tx_id = str(uuid.uuid4())
        row = _make_tx_row(tx_id=tx_id)
        prisma = _make_prisma_client(rows=[row])
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import get_x402_transaction

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            result = await get_x402_transaction(tx_id=tx_id, user_api_key_dict=admin)

        assert result.tx_id == tx_id

    @pytest.mark.asyncio
    async def test_missing_transaction_returns_404(self):
        from fastapi import HTTPException

        prisma = _make_prisma_client(rows=[])
        prisma.db.litellm_x402transactions.find_unique = AsyncMock(return_value=None)
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import get_x402_transaction

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            with pytest.raises(HTTPException) as exc_info:
                await get_x402_transaction(tx_id="nonexistent", user_api_key_dict=admin)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests — update transaction
# ---------------------------------------------------------------------------


class TestUpdateTransaction:
    """PATCH /x402/transactions/{tx_id}"""

    @pytest.mark.asyncio
    async def test_updates_status_to_settled(self):
        tx_id = str(uuid.uuid4())
        settled_row = _make_tx_row(tx_id=tx_id, status="settled", tx_hash="0xnewhash")
        prisma = _make_prisma_client(rows=[settled_row])
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import update_x402_transaction
        from litellm.types.x402 import X402TransactionUpdate

        body = X402TransactionUpdate(status="settled", tx_hash="0xnewhash")

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            result = await update_x402_transaction(
                tx_id=tx_id, body=body, user_api_key_dict=admin
            )

        assert result.status == "settled"
        assert result.tx_hash == "0xnewhash"

    @pytest.mark.asyncio
    async def test_empty_update_returns_400(self):
        from fastapi import HTTPException

        prisma = _make_prisma_client(rows=[])
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import update_x402_transaction
        from litellm.types.x402 import X402TransactionUpdate

        body = X402TransactionUpdate()  # all fields None

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            with pytest.raises(HTTPException) as exc_info:
                await update_x402_transaction(
                    tx_id="some-id", body=body, user_api_key_dict=admin
                )
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Tests — retry transaction
# ---------------------------------------------------------------------------


class TestRetryTransaction:
    """POST /x402/transactions/{tx_id}/retry"""

    @pytest.mark.asyncio
    async def test_failed_transaction_can_be_retried(self):
        tx_id = str(uuid.uuid4())
        failed_row = _make_tx_row(tx_id=tx_id, status="failed", retry_count=0)
        retried_row = _make_tx_row(tx_id=tx_id, status="pending", retry_count=1)
        prisma = _make_prisma_client(rows=[failed_row])
        prisma.db.litellm_x402transactions.update = AsyncMock(return_value=retried_row)
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import retry_x402_transaction

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            result = await retry_x402_transaction(tx_id=tx_id, user_api_key_dict=admin)

        assert result.status == "pending"
        assert result.retry_count == 1

    @pytest.mark.asyncio
    async def test_non_failed_transaction_returns_400(self):
        from fastapi import HTTPException

        tx_id = str(uuid.uuid4())
        settled_row = _make_tx_row(tx_id=tx_id, status="settled")
        prisma = _make_prisma_client(rows=[settled_row])
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import retry_x402_transaction

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            with pytest.raises(HTTPException) as exc_info:
                await retry_x402_transaction(tx_id=tx_id, user_api_key_dict=admin)

        assert exc_info.value.status_code == 400
        assert "failed" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_missing_transaction_returns_404(self):
        from fastapi import HTTPException

        prisma = _make_prisma_client(rows=[])
        prisma.db.litellm_x402transactions.find_unique = AsyncMock(return_value=None)
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import retry_x402_transaction

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            with pytest.raises(HTTPException) as exc_info:
                await retry_x402_transaction(tx_id="nonexistent", user_api_key_dict=admin)

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Tests — stats
# ---------------------------------------------------------------------------


class TestStats:
    """GET /x402/stats"""

    @pytest.mark.asyncio
    async def test_returns_stats_response(self):
        rows = [
            _make_tx_row(status="settled", amount_usd=0.01, model="gpt-4o"),
            _make_tx_row(status="settled", amount_usd=0.02, model="gpt-4o"),
            _make_tx_row(status="failed", amount_usd=0.01, model="claude-3-5-sonnet"),
        ]
        prisma = _make_prisma_client(rows=rows, total=3)
        prisma.db.litellm_x402transactions.count = AsyncMock(return_value=0)
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import get_x402_stats
        from litellm.types.x402 import X402StatsResponse

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            result = await get_x402_stats(
                user_api_key_dict=admin, top_models_limit=10
            )

        assert isinstance(result, X402StatsResponse)
        assert result.daily.transaction_count >= 0
        assert result.weekly.transaction_count >= 0
        assert result.monthly.transaction_count >= 0

    @pytest.mark.asyncio
    async def test_stats_top_models_sorted_by_count(self):
        rows = [
            _make_tx_row(model="gpt-4o", amount_usd=0.01),
            _make_tx_row(model="gpt-4o", amount_usd=0.01),
            _make_tx_row(model="claude-3-5-sonnet", amount_usd=0.02),
        ]
        prisma = _make_prisma_client(rows=rows, total=3)
        prisma.db.litellm_x402transactions.count = AsyncMock(return_value=0)
        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import get_x402_stats

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", prisma):
            result = await get_x402_stats(
                user_api_key_dict=admin, top_models_limit=10
            )

        if result.top_models:
            # Top model should have the highest transaction count
            assert result.top_models[0].transaction_count >= result.top_models[-1].transaction_count

    @pytest.mark.asyncio
    async def test_stats_503_when_no_db(self):
        from fastapi import HTTPException

        admin = _make_admin_key()

        from litellm.proxy.x402_endpoints.endpoints import get_x402_stats

        with patch("litellm.proxy.x402_endpoints.endpoints.prisma_client", None):
            with pytest.raises(HTTPException) as exc_info:
                await get_x402_stats(user_api_key_dict=admin, top_models_limit=10)

        assert exc_info.value.status_code == 503
