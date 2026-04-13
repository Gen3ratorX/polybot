from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.gamma import GammaClient
from bot.autoscale import AutoScaleEngine
from bot.config import load_environment, load_runtime_config
from bot.ranker import EdgeRanker, RankedMarket
from bot.scanner import MarketScanner
from bot.tracker import TradeTracker
from models import Market, Trade


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    initial_bankroll: float
    final_bankroll: float
    qualified_count: int
    ranked_count: int
    executed_trades: int
    wins: int
    losses: int
    total_pnl: float

    def as_dict(self) -> dict[str, object]:
        return {
            "initial_bankroll": self.initial_bankroll,
            "final_bankroll": self.final_bankroll,
            "qualified_count": self.qualified_count,
            "ranked_count": self.ranked_count,
            "executed_trades": self.executed_trades,
            "wins": self.wins,
            "losses": self.losses,
            "total_pnl": self.total_pnl,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a deterministic paper-style strategy simulation over a market snapshot."
    )
    parser.add_argument("--snapshot", type=Path, default=None, help="Optional Gamma markets JSON snapshot")
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--initial-bankroll", type=float, default=25.0)
    parser.add_argument("--trade-size-usd", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--write-db", action="store_true", help="Persist simulated trades and states to SQLite")
    parser.add_argument(
        "--strategy-section",
        default="late_market_edge",
        help="Which strategy profile to use. Defaults to late_market_edge.",
    )
    return parser.parse_args()


async def load_markets(snapshot_path: Path | None) -> list[Market]:
    if snapshot_path is not None:
        payload = json.loads(snapshot_path.read_text())
        raw_markets = payload["markets"] if isinstance(payload, dict) and "markets" in payload else payload
        if not isinstance(raw_markets, list):
            raise ValueError("snapshot must contain a JSON list or an object with a 'markets' list")
        return [Market.from_gamma_market(item) for item in raw_markets]

    async with GammaClient() as gamma:
        return await gamma.fetch_all_open_markets()


def simulate_trade(
    ranked_market: RankedMarket,
    *,
    bankroll: float,
    trade_size_usd: float,
    rng: random.Random,
    strategy_name: str | None = None,
) -> Trade:
    position_size = min(trade_size_usd, bankroll)
    trade = Trade(
        timestamp=ranked_market.market.end_date,
        market_id=ranked_market.market.market_id,
        market_question=ranked_market.market.question,
        category=ranked_market.market.category,
        side=ranked_market.selected_side,
        entry_price=ranked_market.selected_price,
        position_size=position_size,
        score=ranked_market.score,
        hours_to_close=ranked_market.hours_to_close,
        paper_trade=True,
        strategy_name=strategy_name,
    )
    resolution_price = 1.0 if rng.random() < ranked_market.selected_price else 0.0
    return trade.settle(resolution_price=resolution_price, settled_at=ranked_market.market.end_date)


def run_backtest(
    markets: list[Market],
    *,
    cycles: int,
    initial_bankroll: float,
    trade_size_usd: float,
    seed: int,
    tracker: TradeTracker | None,
    strategy_section: str = "late_market_edge",
) -> tuple[BacktestSummary, list[Trade], list[dict[str, object]]]:
    if cycles <= 0:
        raise ValueError("cycles must be positive")
    if initial_bankroll <= 0:
        raise ValueError("initial_bankroll must be positive")
    if trade_size_usd <= 0:
        raise ValueError("trade_size_usd must be positive")

    runtime = load_runtime_config(strategy_section=strategy_section)
    scanner = MarketScanner(gamma_client=None, config=runtime.strategy)  # type: ignore[arg-type]
    qualifying = scanner.filter_markets(markets)
    ranker = EdgeRanker(runtime.strategy)
    ranked = ranker.rank_markets(qualifying)
    if not ranked:
        summary = BacktestSummary(
            initial_bankroll=initial_bankroll,
            final_bankroll=initial_bankroll,
            qualified_count=len(qualifying),
            ranked_count=0,
            executed_trades=0,
            wins=0,
            losses=0,
            total_pnl=0.0,
        )
        return summary, [], []

    rng = random.Random(seed)
    bankroll = initial_bankroll
    trades: list[Trade] = []
    trade_summaries: list[dict[str, object]] = []
    autoscale = AutoScaleEngine()

    for index in range(cycles):
        ranked_market = ranked[index % len(ranked)]
        resolved_trade = simulate_trade(
            ranked_market,
            bankroll=bankroll,
            trade_size_usd=trade_size_usd,
            rng=rng,
            strategy_name=runtime.strategy.name,
        )
        bankroll = round(bankroll + (resolved_trade.pnl or 0.0), 6)
        trades.append(resolved_trade)
        trade_summaries.append(
            {
                "market_id": resolved_trade.market_id,
                "question": resolved_trade.market_question,
                "side": resolved_trade.side.value,
                "score": resolved_trade.score,
                "entry_price": resolved_trade.entry_price,
                "position_size": resolved_trade.position_size,
                "outcome": resolved_trade.outcome.value,
                "pnl": resolved_trade.pnl,
            }
        )
        if tracker is not None:
            tracker.record_trade(resolved_trade)
            state = tracker.build_state_snapshot(
                bankroll=bankroll,
                phase=autoscale.get_phase_info(bankroll).phase,
                strategy_min_price=runtime.strategy.min_price,
                strategy_min_score=runtime.strategy.min_score,
                strategy_name=runtime.strategy.name,
            )
            tracker.record_state(state)

    wins = sum(1 for trade in trades if (trade.pnl or 0.0) > 0)
    losses = sum(1 for trade in trades if (trade.pnl or 0.0) <= 0)
    total_pnl = round(sum(trade.pnl or 0.0 for trade in trades), 6)
    summary = BacktestSummary(
        initial_bankroll=initial_bankroll,
        final_bankroll=bankroll,
        qualified_count=len(qualifying),
        ranked_count=len(ranked),
        executed_trades=len(trades),
        wins=wins,
        losses=losses,
        total_pnl=total_pnl,
    )
    return summary, trades, trade_summaries


def main() -> None:
    args = parse_args()
    env = load_environment()
    tracker = TradeTracker(env.database_url) if args.write_db else None
    if tracker is not None:
        tracker.initialize()

    markets = asyncio.run(load_markets(args.snapshot))
    summary, _, trade_summaries = run_backtest(
        markets,
        cycles=args.cycles,
        initial_bankroll=args.initial_bankroll,
        trade_size_usd=args.trade_size_usd,
        seed=args.seed,
        tracker=tracker,
        strategy_section=args.strategy_section,
    )
    print(json.dumps({"summary": summary.as_dict()}, indent=2))
    print(json.dumps({"trades": trade_summaries[: min(10, len(trade_summaries))]}, indent=2))


if __name__ == "__main__":
    main()
