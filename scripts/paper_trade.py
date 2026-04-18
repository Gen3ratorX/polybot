from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.gamma import GammaClient
from bot.catalyst import load_active_catalyst_snapshot
from bot.config import load_environment, load_runtime_config
from bot.paper import PaperTradingEngine
from bot.ranker import EdgeRanker
from bot.risk import RiskManager
from bot.runtime_state import send_optional_alert
from bot.scanner import MarketScanner
from bot.spot import load_active_spot_snapshot, load_active_spot_snapshots
from bot.tracker import TradeTracker


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run paper trading over live Gamma markets.")
    parser.add_argument(
        "--strategy-section",
        default="late_market_edge",
        help="Which strategy profile to use. Defaults to late_market_edge.",
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=20,
        help="How many paper cycles to run. Defaults to 20 so momentum profiles have time to print trades.",
    )
    parser.add_argument(
        "--trade-size-usd",
        type=float,
        default=None,
        help="Override the paper trade size. Defaults to the smaller of the profile cap and the global min position.",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Optional session identifier. Defaults to a generated UUID4 hex string.",
    )
    parser.add_argument(
        "--drain-sleep-seconds",
        type=float,
        default=0.25,
        help="Sleep interval while draining delayed paper positions.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    runtime = load_runtime_config(strategy_section=args.strategy_section)
    paper_runtime = _paper_runtime(runtime)
    env = load_environment()
    tracker = TradeTracker(env.database_url)
    tracker.initialize()
    session_id = args.session_id or uuid4().hex

    async with GammaClient() as gamma_client:
        scanner = MarketScanner(gamma_client, paper_runtime.strategy)
        ranker = EdgeRanker(paper_runtime.strategy)
        engine = PaperTradingEngine(
            scanner=scanner,
            ranker=ranker,
            tracker=tracker,
            risk_manager=RiskManager(),
            initial_bankroll=20.0,
            trade_size_usd=_paper_trade_size(paper_runtime, args.trade_size_usd),
            strategy_name=paper_runtime.strategy.name,
            settlement_delay_minutes=_paper_settlement_delay_minutes(paper_runtime),
            session_id=session_id,
            open_position_alert=lambda message: send_optional_alert(env, message, level="INFO", dedupe_window_seconds=0.0),
        )
        current = datetime.now(UTC)
        state = engine.state
        seen_market_ids: set[str] = set()
        for _ in range(args.cycles):
            market_universe = await scanner.load_markets()
            spot_snapshot = await _load_cycle_spot_snapshot(env=env, runtime=paper_runtime, tracker=tracker)
            catalyst_snapshot = await load_active_catalyst_snapshot(env=env, runtime=paper_runtime, tracker=tracker)
            result = await engine.run_cycle(
                as_of=current,
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
                blocked_market_ids=seen_market_ids,
                market_universe=market_universe,
            )
            state = result.state
            if result.trade is not None:
                seen_market_ids.add(result.trade.market_id)
            if result.kill_signal is not None:
                break
            current += timedelta(minutes=1)

        state = await _drain_paper_session(
            engine=engine,
            scanner=scanner,
            env=env,
            runtime=paper_runtime,
            tracker=tracker,
            current=current,
            sleep_seconds=args.drain_sleep_seconds,
        )

    print(f"session_id={session_id}")
    print(f"paper trades={state.total_trades}")
    print(f"bankroll={state.bankroll:.4f}")


def _paper_trade_size(runtime, override: float | None) -> float:
    if override is not None:
        if override <= 0:
            raise ValueError("trade size override must be positive")
        return override
    profile_cap = runtime.strategy.max_position_usd
    if profile_cap is not None:
        return min(profile_cap, runtime.execution.min_position_usd)
    return runtime.execution.min_position_usd


def _paper_strategy_name(runtime) -> str:
    return runtime.strategy.name


def _paper_settlement_delay_minutes(runtime) -> int:
    if runtime.strategy.signal_mode == "momentum":
        return min(10, max(1, runtime.strategy.exit_rules.max_hold_minutes_without_progress))
    return 0


def _paper_runtime(runtime):
    if runtime.strategy.name not in {"hourly_momentum_multi_asset", "btc_momentum_scalp"}:
        return runtime
    signal_rules = runtime.strategy.signal_rules
    if signal_rules is None:
        return runtime
    relaxed_strategy = replace(
        runtime.strategy,
        filter_bounds=replace(runtime.strategy.filter_bounds, min_volume=0.0),
        flow_risk_thresholds=replace(runtime.strategy.flow_risk_thresholds, min_liquidity=0.0),
        signal_rules=replace(
            signal_rules,
            spot_min_abs_return_1h_pct=None,
            spot_min_abs_return_15m_pct=None,
            spot_min_contract_lag_pct=None,
            momentum_min_abs_volume_change_1h_pct=None,
            momentum_min_abs_one_hour_price_change=None,
        ),
        min_score=min(runtime.strategy.min_score, 3.5),
    )
    return replace(runtime, strategy=relaxed_strategy)


async def _load_cycle_spot_snapshot(*, env, runtime, tracker):
    if runtime.strategy.spot_symbols:
        snapshots = await load_active_spot_snapshots(env=env, runtime=runtime, tracker=tracker)
        return snapshots if snapshots else None
    return await load_active_spot_snapshot(env=env, runtime=runtime, tracker=tracker)


async def _drain_paper_session(
    *,
    engine: PaperTradingEngine,
    scanner: MarketScanner,
    env,
    runtime,
    tracker: TradeTracker,
    current: datetime,
    sleep_seconds: float,
) -> object:
    if sleep_seconds < 0:
        raise ValueError("sleep_seconds must be non-negative")
    drain_cycles = 0
    max_drain_cycles = max(1, _paper_settlement_delay_minutes(runtime) + 5)
    while engine.state.open_positions > 0 and drain_cycles < max_drain_cycles:
        market_universe = await scanner.load_markets()
        spot_snapshot = await _load_cycle_spot_snapshot(env=env, runtime=runtime, tracker=tracker)
        catalyst_snapshot = await load_active_catalyst_snapshot(env=env, runtime=runtime, tracker=tracker)
        result = await engine.run_cycle(
            as_of=current,
            spot_snapshot=spot_snapshot,
            catalyst_snapshot=catalyst_snapshot,
            market_universe=market_universe,
            drain_only=True,
        )
        if result.kill_signal is not None:
            break
        current += timedelta(minutes=1)
        drain_cycles += 1
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)
    return engine.state


if __name__ == "__main__":
    asyncio.run(main())
