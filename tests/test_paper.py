from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import load_runtime_config
from bot.paper import PaperExecutionSimulator, PaperFillMode, PaperTradingEngine
from bot.ranker import EdgeRanker
from bot.risk import RiskManager
from bot.tracker import TradeTracker
from models import Market


class StubScanner:
    def __init__(self, market: Market) -> None:
        self.market = market
        self.calls = 0

    async def scan(self, *, as_of: datetime | None = None) -> list[Market]:
        self.calls += 1
        return [self.market]


@pytest.mark.asyncio
async def test_paper_trading_engine_runs_100_cycles_and_logs_trades(tmp_path) -> None:
    config = load_runtime_config("config.yaml")
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'paper.db'}")
    tracker.initialize()
    scanner = StubScanner(_market())
    engine = PaperTradingEngine(
        scanner=scanner,
        ranker=EdgeRanker(config.strategy),
        tracker=tracker,
        risk_manager=RiskManager(),
        initial_bankroll=20.0,
        trade_size_usd=1.0,
        strategy_name="late_market_edge",
        resolver=lambda ranked_market: 1.0,
        execution_simulator=PaperExecutionSimulator(mode=PaperFillMode.FULL_FILL),
    )

    state = await engine.run(
        cycles=100,
        start_at=datetime(2026, 4, 6, 12, 0, tzinfo=UTC),
        step=timedelta(minutes=1),
    )

    latest_state = tracker.get_latest_state()

    assert state.total_trades == 100
    assert tracker.trade_count() == 100
    assert tracker.trade_count(strategy_name="late_market_edge") == 100
    assert tracker.open_order_count() == 0
    assert latest_state is not None
    assert latest_state.strategy_name == "late_market_edge"
    assert latest_state.total_trades == 100
    assert latest_state.bankroll > 20.0
    assert scanner.calls == 100


@pytest.mark.asyncio
async def test_paper_trading_engine_records_partial_fill_and_resting_order(tmp_path) -> None:
    config = load_runtime_config("config.yaml")
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'paper-partial.db'}")
    tracker.initialize()
    scanner = StubScanner(_market())
    engine = PaperTradingEngine(
        scanner=scanner,
        ranker=EdgeRanker(config.strategy),
        tracker=tracker,
        risk_manager=RiskManager(),
        initial_bankroll=20.0,
        trade_size_usd=1.0,
        strategy_name="late_market_edge",
        resolver=lambda ranked_market: 1.0,
        execution_simulator=PaperExecutionSimulator(
            mode=PaperFillMode.PARTIAL_FILL,
            partial_fill_ratio=0.5,
        ),
    )

    state = await engine.run_cycle(
        as_of=datetime(2026, 4, 6, 12, 0, tzinfo=UTC),
    )

    latest_state = tracker.get_latest_state()
    open_orders = tracker.list_open_orders()

    assert tracker.trade_count() == 1
    assert tracker.trade_count(strategy_name="late_market_edge") == 1
    assert tracker.open_order_count() == 1
    assert len(open_orders) == 1
    assert open_orders[0]["status"] == "PARTIALLY_FILLED"
    assert open_orders[0]["filled_size"] == 0.5
    assert open_orders[0]["limit_price"] == 0.97
    assert state.trade is not None
    assert state.trade.strategy_name == "late_market_edge"
    assert state.trade.screened_price == 0.97
    assert state.trade.fill_slippage == 0.0
    assert state.state.total_trades == 1
    assert latest_state is not None
    assert latest_state.strategy_name == "late_market_edge"
    assert latest_state.total_trades == 1
    assert latest_state.open_orders == 1
    assert latest_state.bankroll > 20.0


def _market() -> Market:
    return Market(
        market_id="paper-market",
        condition_id="cond-paper-market",
        question="Paper market question",
        end_date=datetime(2026, 4, 6, 15, 0, tzinfo=UTC),
        yes_token_id="yes-paper",
        no_token_id="no-paper",
        yes_price=0.97,
        no_price=0.02,
        volume=1_000,
        category="sports",
        volume_change_1h_pct=12.0,
    )
