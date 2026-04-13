from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.clob import build_clob_client
from api.gamma import GammaClient
from bot.config import load_environment
from bot.executor import OrderExecutor
from bot.process_lock import acquire_database_lock
from models import OutcomeSide


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or place a tiny Polymarket limit buy order."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--market-id", help="Gamma market id to trade")
    target.add_argument("--slug", help="Gamma market slug to trade")
    parser.add_argument("--side", choices=["yes", "no"], required=True)
    parser.add_argument("--budget-usdc", type=float, default=1.0)
    parser.add_argument(
        "--price",
        type=float,
        default=None,
        help="Optional custom limit price. Default uses current best bid.",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Actually submit the live order. Without this flag, only preview.",
    )
    parser.add_argument(
        "--cancel-after",
        type=int,
        default=10,
        help="Seconds to wait before cancelling the order after submission. Use 0 to skip auto-cancel.",
    )
    return parser.parse_args()


def main() -> None:
    try:
        args = parse_args()
        env = load_environment()
        side = OutcomeSide.YES if args.side.lower() == "yes" else OutcomeSide.NO

        import asyncio

        preview = asyncio.run(
            _build_preview(
                env,
                args.market_id,
                args.slug,
                side,
                args.budget_usdc,
                args.price,
            )
        )
        print(json.dumps(preview.as_dict(), indent=2))

        if not args.submit:
            print("preview_only: pass --submit to place this live order")
            return

        with acquire_database_lock(env.database_url):
            client = build_clob_client(env, include_api_creds=True)
            executor = OrderExecutor(client)
            response = executor.place_limit_buy(preview)
            print(json.dumps({"submit_response": response}, indent=2))

            order_id = executor.extract_order_id(response)
            if not order_id:
                print(
                    "submit_warning: could not extract order id; skipping auto-cancel",
                    file=sys.stderr,
                )
                return

            if args.cancel_after > 0:
                time.sleep(args.cancel_after)
                cancel_response = executor.cancel_order(order_id)
                print(json.dumps({"cancel_response": cancel_response}, indent=2))
    except Exception as exc:
        print(f"test_order_error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


async def _build_preview(
    env,
    market_id: str | None,
    slug: str | None,
    side: OutcomeSide,
    budget_usdc: float,
    price: float | None,
):
    async with GammaClient() as gamma:
        if market_id:
            market = await gamma.fetch_market(market_id)
        else:
            market = await gamma.fetch_market_by_slug(slug or "")

    client = build_clob_client(env, include_api_creds=True)
    executor = OrderExecutor(client)
    return executor.preview_limit_buy(
        market,
        side=side,
        budget_usdc=budget_usdc,
        limit_price=price,
    )


if __name__ == "__main__":
    main()
