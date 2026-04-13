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
from api.gamma import GammaClient
from bot.config import load_environment, load_runtime_config
from bot.reconciler import reconcile_manual_exits, reconcile_open_positions
from bot.runtime_state import send_optional_alert, sync_live_state
from bot.tracker import TradeTracker


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile open positions against live market state.")
    parser.add_argument(
        "--strategy-section",
        default="late_market_edge",
        help="Which strategy profile to use. Defaults to late_market_edge.",
    )
    return parser.parse_args()


async def _run() -> None:
    args = parse_args()
    env = load_environment()
    runtime = load_runtime_config(strategy_section=args.strategy_section)
    tracker = TradeTracker(env.database_url)
    tracker.initialize()

    async with GammaClient() as gamma:
        results = await reconcile_open_positions(tracker, gamma)
        manual_exits = await reconcile_manual_exits(
            tracker,
            gamma,
            clob_client=build_clob_client(env, include_api_creds=True),
            wallet_address=env.poly_wallet_address,
        )
        results.extend(manual_exits)

    print(json.dumps({"resolved_positions": len(results)}, indent=2))
    if results:
        print(
            json.dumps(
                {
                    "items": [
                        {
                            "position_id": result.position_id,
                            "order_id": result.order_id,
                            "market_id": result.market_id,
                            "resolution_price": result.resolution_price,
                            "pnl": result.pnl,
                            "outcome": result.outcome.value,
                        }
                        for result in results
                    ]
                },
                indent=2,
            )
        )

    client = build_clob_client(env, include_api_creds=True)
    state_sync = sync_live_state(
        tracker=tracker,
        runtime=runtime,
        env=env,
        clob_client=client,
    )
    print(
        json.dumps(
            {
                "state_snapshot": {
                    "state_id": state_sync.state_id,
                    "bankroll": state_sync.bankroll,
                    "open_positions": state_sync.state.open_positions,
                    "total_trades": state_sync.state.total_trades,
                }
            },
            indent=2,
        )
    )

    if results:
        message = "\n".join(
            [
                "Reconciled closed positions:",
                *[
                    f"{item.market_id} {item.outcome.value} pnl=${item.pnl:.2f}"
                    for item in results
                ],
            ]
        )
        send_optional_alert(env, message, level="INFO")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
