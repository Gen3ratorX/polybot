from __future__ import annotations

from datetime import UTC, datetime

from bot.executor import LiveOrderPreview, LiveOrderStatus, OrderExecutor
from models import Market, OutcomeSide, TradeOutcome


def test_build_trade_record_for_unfilled_cancelled_order() -> None:
    market = _market()
    preview = _preview()
    status = LiveOrderStatus(
        order_id="oid",
        status="CANCELED",
        created_at=datetime.now(UTC),
        market_condition_id="cond",
        token_id="yes-token",
        side="BUY",
        price=0.53,
        original_size=5.66,
        matched_size=0.0,
    )

    trade = OrderExecutor.build_trade_record(market, preview, status)

    assert trade.outcome is TradeOutcome.CANCELLED
    assert trade.position_size == 3.0
    assert trade.screened_price == 0.53


def test_build_trade_record_for_filled_or_partially_filled_order() -> None:
    market = _market()
    preview = _preview()
    status = LiveOrderStatus(
        order_id="oid",
        status="LIVE",
        created_at=datetime.now(UTC),
        market_condition_id="cond",
        token_id="yes-token",
        side="BUY",
        price=0.53,
        original_size=5.66,
        matched_size=5.0,
    )

    trade = OrderExecutor.build_trade_record(market, preview, status)

    assert trade.outcome is TradeOutcome.PENDING
    assert trade.position_size == 2.65
    assert trade.fill_price == 0.53
    assert trade.screened_price == 0.53
    assert trade.fill_slippage == 0.0
    assert trade.fill_slippage_pct == 0.0


def _market() -> Market:
    return Market(
        market_id="540816",
        condition_id="0xcondition",
        question="Question",
        end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_price=0.53,
        no_price=0.47,
        volume=1000,
    )


def _preview() -> LiveOrderPreview:
    return LiveOrderPreview(
        market_id="540816",
        question="Question",
        outcome_side=OutcomeSide.YES.value,
        token_id="yes-token",
        requested_budget_usdc=3.0,
        budget_usdc=3.0,
        limit_price=0.53,
        shares=5.660377,
        midpoint=0.535,
        best_bid=0.53,
        best_ask=0.54,
        tick_size=0.01,
        min_order_size=5.0,
        minimum_budget_usdc=2.65,
    )
