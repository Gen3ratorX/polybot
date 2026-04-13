from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console

from bot.dashboard import DashboardRenderer
from bot.tracker import TradeTracker
from models import BotState, OutcomeSide, Trade, TradeOutcome


def test_dashboard_renderer_outputs_summary_and_recent_trades(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'dashboard.db'}")
    tracker.initialize()

    trade = Trade(
        timestamp=datetime.now(UTC),
        strategy_name="late_market_edge",
        market_id="m1",
        market_question="Question",
        category="sports",
        side=OutcomeSide.YES,
        entry_price=0.9,
        position_size=9.0,
        outcome=TradeOutcome.CANCELLED,
        order_id="oid",
        paper_trade=False,
    )
    tracker.record_trade(trade)
    tracker.register_open_position_from_trade(
        Trade(
            timestamp=datetime.now(UTC),
            strategy_name="late_market_edge",
            market_id="m2",
            market_question="Open Position Question",
            category="sports",
            side=OutcomeSide.NO,
            entry_price=0.4,
            position_size=2.0,
            outcome=TradeOutcome.PENDING,
            fill_price=0.4,
            fill_time=datetime.now(UTC),
            order_id="open-oid",
            paper_trade=False,
        )
    )
    tracker.record_state(
        BotState(
            timestamp=datetime.now(UTC),
            strategy_name="late_market_edge",
            bankroll=10.0,
            phase=1,
            bankroll_start_of_day=10.0,
            bankroll_start_of_week=10.0,
            total_trades=1,
            open_positions=1,
            recent_trades=(trade,),
        )
    )

    renderer = DashboardRenderer()
    snapshot = renderer.snapshot_from_tracker(tracker)
    renderable = renderer.render(snapshot)
    console = Console(record=True, width=120)
    console.print(renderable)
    output = console.export_text()

    assert "Bankroll" in output
    assert "Question" in output
    assert "late_market_edge" in output
    assert "CANCELLED" in output
    assert "Open Positions" in output
    assert "Open Position Question" in output


def test_dashboard_renderer_falls_back_to_trade_history_without_state(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'dashboard-no-state.db'}")
    tracker.initialize()
    tracker.record_trade(
        Trade(
            timestamp=datetime.now(UTC),
            market_id="m1",
            market_question="Fallback Question",
            category="sports",
            side=OutcomeSide.YES,
            entry_price=0.9,
            position_size=9.0,
            outcome=TradeOutcome.CANCELLED,
            order_id="oid",
            paper_trade=False,
        )
    )

    renderer = DashboardRenderer()
    snapshot = renderer.snapshot_from_tracker(tracker)

    assert snapshot.total_trades == 1
    assert snapshot.recent_trades[0].market_question == "Fallback Question"
