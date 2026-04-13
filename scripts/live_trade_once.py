from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.clob import build_clob_client
from api.gamma import GammaClient
from bot.config import load_environment, load_runtime_config
from bot.executor import OrderExecutor
from bot.process_lock import acquire_database_lock
from bot.order_monitor import monitor_order_status
from bot.runtime_state import send_optional_alert, sync_live_state
from bot.tracker import TradeTracker
from models import OutcomeSide


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Place, monitor, and log a single live Polymarket trade."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--market-id", help="Gamma market id to trade")
    target.add_argument("--slug", help="Gamma market slug to trade")
    parser.add_argument("--side", choices=["yes", "no"], required=True)
    parser.add_argument("--budget-usdc", type=float, required=True)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--monitor-seconds", type=int, default=20)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--cancel-if-open", action="store_true")
    parser.add_argument(
        "--strategy-section",
        default="late_market_edge",
        help="Which strategy profile to use. Defaults to late_market_edge.",
    )
    return parser.parse_args()


def main() -> None:
    try:
        args = parse_args()
        env = load_environment()
        runtime = load_runtime_config(strategy_section=args.strategy_section)
        side = OutcomeSide.YES if args.side.lower() == "yes" else OutcomeSide.NO

        market, preview = asyncio.run(
            _load_market_and_preview(
                env,
                market_id=args.market_id,
                slug=args.slug,
                side=side,
                budget_usdc=args.budget_usdc,
                price=args.price,
            )
        )
        print(json.dumps(preview.as_dict(), indent=2))
        print(
            json.dumps(
                {
                    "budget_summary": {
                        "requested_budget_usdc": preview.requested_budget_usdc,
                        "actual_budget_usdc": preview.budget_usdc,
                        "minimum_budget_usdc": preview.minimum_budget_usdc,
                    }
                },
                indent=2,
            )
        )
        if not args.submit:
            print("preview_only: pass --submit to place and monitor this live trade")
            return

        with acquire_database_lock(env.database_url):
            client = build_clob_client(env, include_api_creds=True)
            executor = OrderExecutor(
                client,
                execution_style=runtime.strategy.execution_style or runtime.execution.execution_style,
            )
            tracker = TradeTracker(env.database_url)
            tracker.initialize()

            submitted_at = datetime.now(UTC)
            submit_response = executor.place_limit_buy(preview)
            print(json.dumps({"submit_response": submit_response}, indent=2))
            order_id = executor.extract_order_id(submit_response)
            if not order_id:
                raise ValueError("Could not extract order id from submit response")
            tracker.record_order(
                order_id=order_id,
                market_id=market.market_id,
                strategy_name=runtime.strategy.name,
                status="OPEN",
                requested_size=preview.budget_usdc,
                limit_price=preview.limit_price,
                filled_size=0.0,
                last_seen_status="OPEN",
                last_seen_at=submitted_at,
                exchange_payload={
                    "submit_response": submit_response,
                    "preview": preview.as_dict(),
                },
            )
            alert = send_optional_alert(
                env,
                (
                    f"Submitted live order for {preview.question}\n"
                    f"Order ID: {order_id}\n"
                    f"Side: {preview.outcome_side}\n"
                    f"Budget: ${preview.budget_usdc:.2f}\n"
                    f"Limit price: {preview.limit_price:.2f}"
                ),
                level="INFO",
            )
            if alert is not None:
                print(json.dumps({"alert_delivery": {"ok": alert.ok, "status": alert.status}}, indent=2))

            final_status = monitor_order_status(
                env=env,
                executor=executor,
                order_id=order_id,
                market_id=market.condition_id,
                timeout_seconds=args.monitor_seconds,
                poll_interval=args.poll_interval,
                heartbeat_seconds=runtime.execution.websocket_heartbeat_seconds,
            )

            if not final_status.is_terminal and args.cancel_if_open:
                cancel_response = executor.cancel_order(order_id)
                print(json.dumps({"cancel_response": cancel_response}, indent=2))
                final_status = executor.get_order_status(order_id)
            final_status = executor.reconcile_fill_status(
                preview,
                final_status,
                submitted_at=submitted_at,
            )

            if final_status.has_fill:
                tracker.update_order_fill(
                    order_id=order_id,
                    market_id=market.market_id,
                    strategy_name=runtime.strategy.name,
                    requested_size=preview.budget_usdc,
                    limit_price=preview.limit_price,
                    filled_size=final_status.matched_size,
                    last_seen_status=final_status.status,
                    last_seen_at=final_status.created_at or submitted_at,
                    exchange_payload={
                        "final_status": {
                            "order_id": final_status.order_id,
                            "status": final_status.status,
                            "matched_size": final_status.matched_size,
                            "original_size": final_status.original_size,
                            "price": final_status.price,
                        }
                    },
                )
            elif final_status.is_terminal:
                tracker.close_order(
                    order_id=order_id,
                    strategy_name=runtime.strategy.name,
                    status=final_status.status,
                    last_seen_status=final_status.status,
                    last_seen_at=final_status.created_at or submitted_at,
                    filled_size=final_status.matched_size,
                    exchange_payload={
                        "final_status": {
                            "order_id": final_status.order_id,
                            "status": final_status.status,
                            "matched_size": final_status.matched_size,
                            "original_size": final_status.original_size,
                            "price": final_status.price,
                        }
                    },
                )
            else:
                tracker.record_order(
                    order_id=order_id,
                    market_id=market.market_id,
                    strategy_name=runtime.strategy.name,
                    status=final_status.status,
                    requested_size=preview.budget_usdc,
                    limit_price=preview.limit_price,
                    filled_size=final_status.matched_size,
                    last_seen_status=final_status.status,
                    last_seen_at=final_status.created_at or submitted_at,
                    exchange_payload={
                        "final_status": {
                            "order_id": final_status.order_id,
                            "status": final_status.status,
                            "matched_size": final_status.matched_size,
                            "original_size": final_status.original_size,
                            "price": final_status.price,
                        }
                    },
                )

        print(
            json.dumps(
                {
                    "final_order_status": {
                        "order_id": final_status.order_id,
                        "status": final_status.status,
                        "matched_size": final_status.matched_size,
                        "original_size": final_status.original_size,
                        "price": final_status.price,
                    }
                },
                indent=2,
            )
        )

        trade = None
        trade_id = None
        position_id = None
        if final_status.has_fill:
            trade = executor.build_trade_record(
                market,
                preview,
                final_status,
                strategy_name=runtime.strategy.name,
            )
            trade_id = tracker.record_trade(
                trade,
                notes=json.dumps(
                    {
                        "strategy_name": runtime.strategy.name,
                        "preview": preview.as_dict(),
                        "final_status": {
                            "order_id": final_status.order_id,
                            "status": final_status.status,
                            "matched_size": final_status.matched_size,
                            "original_size": final_status.original_size,
                            "price": final_status.price,
                        },
                    }
                ),
            )
            position_id = tracker.register_open_position_from_trade(trade)
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
                        "phase": state_sync.state.phase,
                        "total_trades": state_sync.state.total_trades,
                        "open_orders": state_sync.state.open_orders,
                        "open_positions": state_sync.state.open_positions,
                    }
                },
                indent=2,
            )
        )
        if position_id is not None:
            print(json.dumps({"position_id": position_id, "position_status": "OPEN"}, indent=2))
        print(
            json.dumps(
                {
                    "execution_status": "FILLED"
                    if final_status.has_fill
                    else (
                        "RESTING"
                        if final_status.status in {"LIVE", "LIVE_RESTING"}
                        else final_status.status
                    ),
                    "tracked_trade_id": trade_id,
                    "trade_outcome": None if trade is None else trade.outcome.value,
                },
                indent=2,
            )
        )
        send_optional_alert(
            env,
            (
                f"Final order status for {preview.question}\n"
                f"Order ID: {final_status.order_id}\n"
                f"Status: {final_status.status}\n"
                f"Matched size: {final_status.matched_size:.2f}\n"
                f"Tracked trade id: {trade_id}\n"
                f"Execution status: {'FILLED' if final_status.has_fill else ('RESTING' if final_status.status in {'LIVE', 'LIVE_RESTING'} else final_status.status)}"
            ),
            level="WIN" if final_status.has_fill else "INFO" if final_status.status in {"LIVE", "LIVE_RESTING"} else "WARN",
        )
    except Exception as exc:
        print(f"live_trade_once_error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


async def _load_market_and_preview(env, *, market_id, slug, side, budget_usdc, price):
    async with GammaClient() as gamma:
        if market_id:
            market = await gamma.fetch_market(market_id)
        else:
            market = await gamma.fetch_market_by_slug(slug or "")

    client = build_clob_client(env, include_api_creds=True)
    executor = OrderExecutor(client)
    preview = executor.preview_limit_buy(
        market,
        side=side,
        budget_usdc=budget_usdc,
        limit_price=price,
    )
    return market, preview
if __name__ == "__main__":
    main()
