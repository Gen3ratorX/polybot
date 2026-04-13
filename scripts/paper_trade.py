from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.gamma import GammaClient
from bot.config import load_environment, load_runtime_config
from bot.paper import PaperTradingEngine
from bot.ranker import EdgeRanker
from bot.risk import RiskManager
from bot.scanner import MarketScanner
from bot.tracker import TradeTracker


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run paper trading over live Gamma markets.")
    parser.add_argument(
        "--strategy-section",
        default="late_market_edge",
        help="Which strategy profile to use. Defaults to late_market_edge.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    runtime = load_runtime_config(strategy_section=args.strategy_section)
    env = load_environment()
    tracker = TradeTracker(env.database_url)
    tracker.initialize()

    async with GammaClient() as gamma_client:
        scanner = MarketScanner(gamma_client, runtime.strategy)
        ranker = EdgeRanker(runtime.strategy)
        engine = PaperTradingEngine(
            scanner=scanner,
            ranker=ranker,
            tracker=tracker,
            risk_manager=RiskManager(),
            initial_bankroll=20.0,
            trade_size_usd=runtime.execution.min_position_usd,
        )
        state = await engine.run(cycles=5, start_at=datetime.now(UTC))

    print(f"paper trades={state.total_trades}")
    print(f"bankroll={state.bankroll:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
