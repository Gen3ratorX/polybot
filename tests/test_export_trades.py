from __future__ import annotations

from datetime import UTC, datetime

from bot.tracker import TradeTracker
from models import OutcomeSide, Trade, TradeOutcome
from scripts.export_trades import fetch_trade_export_rows, render_trade_export


def test_export_trades_renders_json_and_csv(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'export.db'}")
    tracker.initialize()
    tracker.record_trade(
        Trade(
            timestamp=datetime.now(UTC),
            strategy_name="late_market_edge",
            market_id="m1",
            market_question="Question",
            side=OutcomeSide.YES,
            entry_price=0.9,
            position_size=9.0,
            outcome=TradeOutcome.CANCELLED,
        )
    )

    rows = fetch_trade_export_rows(tracker, limit=10)
    json_rendered = render_trade_export(rows, fmt="json")
    csv_rendered = render_trade_export(rows, fmt="csv")

    assert '"market_id": "m1"' in json_rendered
    assert '"strategy_name": "late_market_edge"' in json_rendered
    assert "market_id" in csv_rendered
    assert "strategy_name" in csv_rendered
    assert "m1" in csv_rendered
