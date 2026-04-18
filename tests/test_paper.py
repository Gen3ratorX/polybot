from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.config import load_runtime_config
from bot.paper import PaperExecutionSimulator, PaperFillMode, PaperTradingEngine
from bot.ranker import EdgeRanker
from bot.risk import RiskManager
from bot.tracker import TradeTracker
from models import Market
from scripts.paper_trade import _paper_runtime, _paper_strategy_name


class StubScanner:
    def __init__(self, market: Market) -> None:
        self.market = market
        self.calls = 0

    async def scan(self, *, as_of: datetime | None = None) -> list[Market]:
        self.calls += 1
        return [self.market]


class SpotAwareStubScanner:
    def __init__(self, market: Market) -> None:
        self.market = market
        self.calls = 0
        self.last_spot_snapshot = None
        self.last_catalyst_snapshot = None

    async def scan(
        self,
        *,
        as_of: datetime | None = None,
        spot_snapshot=None,
        catalyst_snapshot=None,
    ) -> list[Market]:
        self.calls += 1
        self.last_spot_snapshot = spot_snapshot
        self.last_catalyst_snapshot = catalyst_snapshot
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


@pytest.mark.asyncio
async def test_paper_trading_engine_forwards_spot_and_catalyst_snapshots(tmp_path) -> None:
    config = load_runtime_config("config.yaml", strategy_section="hourly_momentum_multi_asset")
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'paper-snapshots.db'}")
    tracker.initialize()
    scanner = SpotAwareStubScanner(_market())
    engine = PaperTradingEngine(
        scanner=scanner,
        ranker=EdgeRanker(config.strategy),
        tracker=tracker,
        risk_manager=RiskManager(),
        initial_bankroll=20.0,
        trade_size_usd=1.0,
        strategy_name="hourly_momentum_multi_asset",
        resolver=lambda ranked_market: 1.0,
        execution_simulator=PaperExecutionSimulator(mode=PaperFillMode.FULL_FILL),
    )

    spot_snapshot = {"ETHUSD": object()}
    catalyst_snapshot = object()

    await engine.run_cycle(
        as_of=datetime(2026, 4, 6, 12, 0, tzinfo=UTC),
        spot_snapshot=spot_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )

    assert scanner.calls == 1
    assert scanner.last_spot_snapshot == spot_snapshot
    assert scanner.last_catalyst_snapshot == catalyst_snapshot


def test_paper_runtime_relaxes_hourly_momentum_profile() -> None:
    runtime = load_runtime_config("config.yaml", strategy_section="hourly_momentum_multi_asset")
    paper_runtime = _paper_runtime(runtime)

    assert runtime.strategy.min_volume == 3000.0
    assert runtime.strategy.min_liquidity == 8000.0
    assert runtime.strategy.spot_min_abs_return_1h_pct == 0.004
    assert runtime.strategy.spot_min_abs_return_15m_pct == 0.0015
    assert runtime.strategy.spot_min_contract_lag_pct == 0.0025
    assert runtime.strategy.momentum_min_abs_volume_change_1h_pct == 8.0
    assert runtime.strategy.momentum_min_abs_one_hour_price_change == 0.004

    assert paper_runtime.strategy.name == "hourly_momentum_multi_asset"
    assert paper_runtime.strategy.min_volume == 0.0
    assert paper_runtime.strategy.min_liquidity == 0.0
    assert paper_runtime.strategy.spot_min_abs_return_1h_pct is None
    assert paper_runtime.strategy.spot_min_abs_return_15m_pct is None
    assert paper_runtime.strategy.spot_min_contract_lag_pct is None
    assert paper_runtime.strategy.momentum_min_abs_volume_change_1h_pct is None
    assert paper_runtime.strategy.momentum_min_abs_one_hour_price_change is None
    assert paper_runtime.strategy.min_score == 3.5


def test_paper_strategy_name_tracks_runtime_profile() -> None:
    runtime = load_runtime_config("config.yaml", strategy_section="hourly_momentum_multi_asset")
    assert _paper_strategy_name(runtime) == "hourly_momentum_multi_asset"


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
