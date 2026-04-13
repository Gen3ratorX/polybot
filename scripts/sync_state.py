from __future__ import annotations

import asyncio
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.clob import build_clob_client
from bot.config import load_environment, load_runtime_config
from bot.reconciler import reconcile_manual_exits
from bot.runtime_state import sync_live_state
from bot.tracker import TradeTracker


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile manual exits and sync live bot state.")
    parser.add_argument(
        "--strategy-section",
        default="late_market_edge",
        help="Which strategy profile to use. Defaults to late_market_edge.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env = load_environment()
    runtime = load_runtime_config(strategy_section=args.strategy_section)
    tracker = TradeTracker(env.database_url)
    tracker.initialize()
    client = build_clob_client(env, include_api_creds=True)
    manual_exit_count = asyncio.run(_reconcile_manual_exits(tracker, env, client))
    result = sync_live_state(
        tracker=tracker,
        runtime=runtime,
        env=env,
        clob_client=client,
    )
    print(
        json.dumps(
            {
                "state_id": result.state_id,
                "bankroll": result.bankroll,
                "phase": result.state.phase,
                "total_trades": result.state.total_trades,
                "open_orders": result.state.open_orders,
                "open_positions": result.state.open_positions,
                "manual_exits_reconciled": manual_exit_count,
            },
            indent=2,
        )
    )


async def _reconcile_manual_exits(tracker, env, client) -> int:
    from api.gamma import GammaClient

    async with GammaClient() as gamma:
        results = await reconcile_manual_exits(
            tracker,
            gamma,
            clob_client=client,
            wallet_address=env.poly_wallet_address,
        )
    return len(results)


if __name__ == "__main__":
    main()
