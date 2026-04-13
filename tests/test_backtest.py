from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.tracker import TradeTracker
from models import Market
from scripts.backtest import run_backtest


def test_run_backtest_produces_summary_and_persists_when_tracker_present(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'backtest.db'}")
    tracker.initialize()

    summary, trades, trade_summaries = run_backtest(
        [_market("a"), _market("b")],
        cycles=2,
        initial_bankroll=10.0,
        trade_size_usd=1.0,
        seed=3,
        tracker=tracker,
    )

    assert summary.executed_trades == 2
    assert len(trades) == 2
    assert len(trade_summaries) == 2
    assert tracker.trade_count() == 2
    assert tracker.get_latest_state() is not None


def _market(market_id: str) -> Market:
    return Market(
        market_id=market_id,
        condition_id=f"cond-{market_id}",
        question=f"Question {market_id}",
        end_date=datetime.now(UTC) + timedelta(hours=3),
        yes_token_id=f"yes-{market_id}",
        no_token_id=f"no-{market_id}",
        yes_price=0.97,
        no_price=0.02,
        volume=1500,
        category="sports",
        volume_change_1h_pct=12.0,
    )
