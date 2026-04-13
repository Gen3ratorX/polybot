from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.tracker import TradeTracker
from models import BotState, OutcomeSide, PositionStatus, Trade, TradeOutcome


def test_tracker_initializes_and_roundtrips_trade_and_state(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'bot.db'}")
    tracker.initialize()

    resolved_trade = Trade(
        timestamp=datetime.now(UTC) - timedelta(minutes=10),
        strategy_name="late_market_edge",
        market_id="m1",
        market_question="Will X happen?",
        category="sports",
        side=OutcomeSide.YES,
        entry_price=0.9,
        screened_price=0.91,
        position_size=9.0,
        score=8.1,
        hours_to_close=2.5,
        outcome=TradeOutcome.WIN,
        resolution_price=1.0,
        pnl=1.0,
        execution_fee=0.1,
        paper_trade=True,
    )
    trade_id = tracker.record_trade(resolved_trade, notes="test trade")

    state = BotState(
        timestamp=datetime.now(UTC),
        bankroll=21.0,
        phase=1,
        bankroll_start_of_day=20.0,
        bankroll_start_of_week=20.0,
        daily_pnl=1.0,
        weekly_pnl=1.0,
        total_trades=1,
        recent_trades=(resolved_trade,),
        strategy_name="late_market_edge",
    )
    state_id = tracker.record_state(state)

    stored_trades = tracker.list_recent_trades(limit=10)
    latest_state = tracker.get_latest_state()

    assert trade_id == 1
    assert state_id == 1
    assert len(stored_trades) == 1
    assert stored_trades[0].market_id == "m1"
    assert stored_trades[0].strategy_name == "late_market_edge"
    assert stored_trades[0].screened_price == 0.91
    assert stored_trades[0].fill_slippage is None
    assert latest_state is not None
    assert latest_state.bankroll == 21.0
    assert latest_state.recent_trades[0].pnl == 1.0


def test_tracker_computes_windowed_and_all_time_win_rates(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'rates.db'}")
    tracker.initialize()

    trades = [
        _resolved_trade("win-1", 1.0),
        _resolved_trade("loss-1", -1.0),
        _resolved_trade("win-2", 2.0),
    ]
    for trade in trades:
        tracker.record_trade(trade)

    assert tracker.trade_count() == 3
    assert tracker.win_rate(2) == 0.5
    assert tracker.win_rate_all() == 2 / 3


def test_tracker_separates_profile_ledgers(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'profiles.db'}")
    tracker.initialize()

    late_trade = _resolved_trade("late-1", 1.0, strategy_name="late_market_edge")
    btc_trade = _resolved_trade("btc-1", -1.0, strategy_name="btc_up_down")
    tracker.record_trade(late_trade)
    tracker.record_trade(btc_trade)
    tracker.record_order(
        order_id="late-order",
        market_id="late-1",
        strategy_name="late_market_edge",
        status="OPEN",
        requested_size=5.0,
        limit_price=0.95,
    )
    tracker.record_order(
        order_id="btc-order",
        market_id="btc-1",
        strategy_name="btc_up_down",
        status="OPEN",
        requested_size=5.0,
        limit_price=0.52,
    )

    late_rows = tracker.list_recent_trades(limit=10, strategy_name="late_market_edge")
    btc_rows = tracker.list_recent_trades(limit=10, strategy_name="btc_up_down")
    report = {row["strategy_name"]: row for row in tracker.profile_performance_report()}

    assert tracker.trade_count(strategy_name="late_market_edge") == 1
    assert tracker.trade_count(strategy_name="btc_up_down") == 1
    assert tracker.open_order_count("late_market_edge") == 1
    assert tracker.open_order_count("btc_up_down") == 1
    assert len(late_rows) == 1
    assert len(btc_rows) == 1
    assert report["late_market_edge"]["open_positions"] == 0
    assert report["btc_up_down"]["open_positions"] == 0
    assert report["late_market_edge"]["trade_count"] == 1
    assert report["btc_up_down"]["trade_count"] == 1
    assert report["late_market_edge"]["open_orders"] == 1
    assert report["btc_up_down"]["open_orders"] == 1


def test_tracker_builds_state_snapshot_from_recorded_trades(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'state.db'}")
    tracker.initialize()

    win_trade = _resolved_trade("win-1", 1.0)
    loss_trade = _resolved_trade("loss-1", -1.0)
    tracker.record_trade(win_trade)
    tracker.record_trade(loss_trade)

    state = tracker.build_state_snapshot(
        bankroll=20.0,
        phase=1,
        strategy_min_price=0.85,
        strategy_min_score=7.0,
        open_orders=0,
        open_positions=0,
    )

    assert state.total_trades == 2
    assert state.daily_pnl == 0.0
    assert state.weekly_pnl == 0.0
    assert state.bankroll_start_of_day == 20.0
    assert state.consecutive_failures == 1


def test_tracker_registers_and_lists_open_positions(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'positions.db'}")
    tracker.initialize()

    pending_trade = Trade(
        timestamp=datetime.now(UTC),
        market_id="m1",
        market_question="Will X happen?",
        category="sports",
        side=OutcomeSide.YES,
        entry_price=0.53,
        position_size=2.65,
        order_id="oid",
        fill_price=0.53,
        fill_time=datetime.now(UTC),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(pending_trade)
    position_id = tracker.register_open_position_from_trade(pending_trade)

    open_positions = tracker.list_open_positions()

    assert position_id == 1
    assert tracker.open_position_count() == 1
    assert open_positions[0].status is PositionStatus.OPEN
    assert open_positions[0].market_id == "m1"


def test_tracker_records_and_merges_orders_idempotently(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'orders.db'}")
    tracker.initialize()

    tracker.record_order(
        order_id="oid-1",
        market_id="m1",
        status="OPEN",
        requested_size=5.0,
        limit_price=0.95,
        filled_size=0.0,
        last_seen_status="OPEN",
        exchange_payload={"status": "OPEN"},
    )
    tracker.record_order(
        order_id="oid-1",
        market_id="m1",
        status="OPEN",
        requested_size=5.0,
        limit_price=0.95,
        filled_size=1.5,
        last_seen_status="OPEN",
        exchange_payload={"status": "OPEN", "filled_size": 1.5},
    )

    open_orders = tracker.list_open_orders()
    stored = tracker.get_order_by_order_id("oid-1")

    assert tracker.open_order_count() == 1
    assert len(open_orders) == 1
    assert stored is not None
    assert stored["limit_price"] == 0.95
    assert stored["filled_size"] == 1.5
    assert stored["status"] == "PARTIALLY_FILLED"


def test_tracker_closes_orders_without_losing_fill_state(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'orders-close.db'}")
    tracker.initialize()

    tracker.record_order(
        order_id="oid-2",
        market_id="m2",
        status="OPEN",
        requested_size=5.0,
        limit_price=0.92,
        filled_size=2.0,
        last_seen_status="OPEN",
        exchange_payload={"status": "OPEN"},
    )
    tracker.close_order(
        order_id="oid-2",
        status="CANCELLED",
        last_seen_status="CANCELLED",
        exchange_payload={"status": "CANCELLED"},
    )

    stored = tracker.get_order_by_order_id("oid-2")

    assert tracker.open_order_count() == 0
    assert stored is not None
    assert stored["limit_price"] == 0.92
    assert stored["filled_size"] == 2.0
    assert stored["status"] == "CANCELLED"


def test_tracker_persists_trade_fill_quality_fields(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'quality.db'}")
    tracker.initialize()

    trade = Trade(
        timestamp=datetime.now(UTC),
        market_id="m-quality",
        market_question="Will X happen?",
        category="sports",
        side=OutcomeSide.YES,
        entry_price=0.95,
        screened_price=0.95,
        position_size=4.85,
        fill_price=0.97,
        fill_time=datetime.now(UTC),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(trade)

    stored = tracker.list_recent_trades(limit=1)[0]

    assert stored.screened_price == pytest.approx(0.95, rel=1e-6)
    assert stored.fill_price == pytest.approx(0.97, rel=1e-6)
    assert stored.fill_slippage == pytest.approx(0.02, rel=1e-6)
    assert stored.fill_slippage_pct == pytest.approx(0.021053, rel=1e-6)


def _resolved_trade(market_id: str, pnl: float, *, strategy_name: str | None = None) -> Trade:
    outcome = TradeOutcome.WIN if pnl > 0 else TradeOutcome.LOSS
    resolution_price = 1.0 if pnl > 0 else 0.0
    return Trade(
        timestamp=datetime.now(UTC),
        market_id=market_id,
        market_question=f"Question {market_id}",
        side=OutcomeSide.YES,
        entry_price=0.9,
        position_size=9.0,
        outcome=outcome,
        resolution_price=resolution_price,
        pnl=pnl,
        strategy_name=strategy_name,
    )
