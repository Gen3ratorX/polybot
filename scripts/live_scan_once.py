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
from bot.candidate_pool import apply_candidate_caps, build_candidate_clusters, distinct_ranked_candidates
from bot.config import load_environment, load_runtime_config
from bot.catalyst import load_active_catalyst_snapshot
from bot.executor import OrderExecutor
from bot.process_lock import acquire_database_lock
from bot.order_monitor import monitor_order_status
from bot.spot import load_active_spot_snapshot
from bot.ranker import EdgeRanker, RankedMarket
from bot.runtime_state import send_optional_alert, sync_live_state
from bot.scanner import MarketScanner
from bot.tracker import TradeTracker


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan live Polymarket markets once, rank candidates, and optionally place one controlled trade."
    )
    parser.add_argument("--budget-usdc", type=float, required=True)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--top", type=int, default=5, help="How many ranked candidates to print")
    parser.add_argument(
        "--near-misses",
        type=int,
        default=5,
        help="How many near-miss markets to print when nothing qualifies",
    )
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
        tracker = TradeTracker(env.database_url)
        tracker.initialize()

        result = asyncio.run(
            _scan_and_preview(
                env=env,
                runtime=runtime,
                tracker=tracker,
                budget_usdc=args.budget_usdc,
                price=args.price,
                top=args.top,
                near_misses=args.near_misses,
            )
        )

        print(json.dumps({"candidate_count": len(result["ranked"])}, indent=2))
        print(json.dumps({"top_candidates": result["top_candidates"]}, indent=2))
        if result["near_miss_candidates"]:
            print(json.dumps({"near_miss_candidates": result["near_miss_candidates"]}, indent=2))
        if result["preview"] is None:
            print("preview_only: no ranked markets passed all filters", file=sys.stderr)
            return
        print(json.dumps({"selected_preview": result["preview"].as_dict()}, indent=2))
        print(
            json.dumps(
                {
                    "budget_summary": {
                        "requested_budget_usdc": result["preview"].requested_budget_usdc,
                        "actual_budget_usdc": result["preview"].budget_usdc,
                        "minimum_budget_usdc": result["preview"].minimum_budget_usdc,
                    }
                },
                indent=2,
            )
        )

        if not args.submit:
            print("preview_only: pass --submit to place and monitor the selected live trade")
            return

        with acquire_database_lock(env.database_url):
            client = build_clob_client(env, include_api_creds=True)
            executor = OrderExecutor(client, execution_style=runtime.strategy.execution_style or runtime.execution.execution_style)
            submitted_at = datetime.now(UTC)
            spot_snapshot = result.get("spot_snapshot")
            signal_to_submit_seconds = None
            spot_fetch_latency_seconds = None
            if spot_snapshot is not None:
                signal_to_submit_seconds = round((submitted_at - spot_snapshot.observed_at).total_seconds(), 6)
                spot_fetch_latency_seconds = spot_snapshot.fetch_latency_seconds
            submit_response = executor.place_limit_buy(result["preview"])
            print(json.dumps({"submit_response": submit_response}, indent=2))
            order_id = executor.extract_order_id(submit_response)
            if not order_id:
                raise ValueError("Could not extract order id from submit response")
            tracker.record_order(
                order_id=order_id,
                market_id=result["selected"].market.market_id,
                strategy_name=runtime.strategy.name,
                status="OPEN",
                requested_size=result["preview"].budget_usdc,
                limit_price=result["preview"].limit_price,
                filled_size=0.0,
                last_seen_status="OPEN",
                last_seen_at=submitted_at,
                exchange_payload={
                    "submit_response": submit_response,
                    "preview": result["preview"].as_dict(),
                    "selected_candidate": _ranked_market_to_dict(result["selected"]),
                },
            )
            alert = send_optional_alert(
                env,
                (
                    f"Submitted scanner-selected order for {result['preview'].question}\n"
                    f"Order ID: {order_id}\n"
                    f"Side: {result['preview'].outcome_side}\n"
                    f"Budget: ${result['preview'].budget_usdc:.2f}\n"
                    f"Score: {result['selected'].score:.2f}"
                    + (
                        ""
                        if signal_to_submit_seconds is None
                        else f"\nSignal-to-submit: {signal_to_submit_seconds:.3f}s"
                    )
                ),
                level="INFO",
            )
            if alert is not None:
                print(json.dumps({"alert_delivery": {"ok": alert.ok, "status": alert.status}}, indent=2))

            final_status = monitor_order_status(
                env=env,
                executor=executor,
                order_id=order_id,
                market_id=result["selected"].market.condition_id,
                timeout_seconds=args.monitor_seconds,
                poll_interval=args.poll_interval,
                heartbeat_seconds=runtime.execution.websocket_heartbeat_seconds,
            )

            if not final_status.is_terminal and args.cancel_if_open:
                cancel_response = executor.cancel_order(order_id)
                print(json.dumps({"cancel_response": cancel_response}, indent=2))
                final_status = executor.get_order_status(order_id)
            final_status = executor.reconcile_fill_status(
                result["preview"],
                final_status,
                submitted_at=submitted_at,
            )

            if final_status.has_fill:
                tracker.update_order_fill(
                    order_id=order_id,
                    market_id=result["selected"].market.market_id,
                    strategy_name=runtime.strategy.name,
                    requested_size=result["preview"].budget_usdc,
                    limit_price=result["preview"].limit_price,
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
                    market_id=result["selected"].market.market_id,
                    strategy_name=runtime.strategy.name,
                    status=final_status.status,
                    requested_size=result["preview"].budget_usdc,
                    limit_price=result["preview"].limit_price,
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
                result["selected"].market,
                result["preview"],
                final_status,
                strategy_name=runtime.strategy.name,
            )
            trade_id = tracker.record_trade(
                trade,
                notes=json.dumps(
                    {
                        "selected_candidate": _ranked_market_to_dict(result["selected"]),
                        "preview": result["preview"].as_dict(),
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
        print(json.dumps({
            "execution_status": "FILLED"
            if final_status.has_fill
            else (
                "RESTING"
                if final_status.status in {"LIVE", "LIVE_RESTING"}
                else final_status.status
            ),
            "tracked_trade_id": trade_id,
            "trade_outcome": None if trade is None else trade.outcome.value,
            "timing": {
                "spot_fetch_latency_seconds": spot_fetch_latency_seconds,
                "catalyst_fetch_latency_seconds": result.get("catalyst_snapshot").fetch_latency_seconds if result.get("catalyst_snapshot") is not None else None,
                "signal_to_submit_seconds": signal_to_submit_seconds,
            },
        }, indent=2))
        send_optional_alert(
            env,
            (
                f"Scanner order finished for {result['preview'].question}\n"
                f"Order ID: {final_status.order_id}\n"
                f"Status: {final_status.status}\n"
                f"Matched size: {final_status.matched_size:.2f}\n"
                f"Tracked trade id: {trade_id}\n"
                f"Execution status: {'FILLED' if final_status.has_fill else ('RESTING' if final_status.status in {'LIVE', 'LIVE_RESTING'} else final_status.status)}"
            ),
            level="WIN" if final_status.has_fill else "INFO" if final_status.status in {"LIVE", "LIVE_RESTING"} else "WARN",
        )
    except Exception as exc:
        print(f"live_scan_once_error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


async def _scan_and_preview(
    *,
    env,
    runtime,
    tracker: TradeTracker,
    budget_usdc: float,
    price: float | None,
    top: int,
    near_misses: int,
):
    spot_snapshot = await load_active_spot_snapshot(env=env, runtime=runtime, tracker=tracker)
    catalyst_snapshot = await load_active_catalyst_snapshot(env=env, runtime=runtime, tracker=tracker)
    async with GammaClient() as gamma:
        scanner = MarketScanner(gamma, runtime.strategy)
        markets = await gamma.fetch_all_open_markets()

    decisions = scanner.diagnose_markets(
        markets,
        spot_snapshot=spot_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )
    qualifying_markets = [decision.market for decision in decisions if decision.qualifies]
    ranker = EdgeRanker(runtime.strategy)
    ranked = ranker.rank_markets(
        qualifying_markets,
        spot_snapshot=spot_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )
    distinct_ranked = distinct_ranked_candidates(ranked)
    capped_ranked = apply_candidate_caps(
        ranked,
        max_per_event=runtime.strategy.max_per_event_candidates,
        max_per_template=runtime.strategy.max_per_template_candidates,
        exact_score_max_per_event=runtime.strategy.exact_score_max_per_event,
    )
    clusters = [item.as_dict() for item in build_candidate_clusters(ranked)[:10]]

    near_miss_candidates = _near_miss_candidates(
        decisions,
        ranker,
        top=max(1, near_misses),
        spot_snapshot=spot_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )

    if not capped_ranked:
        return {
            "ranked": ranked,
            "selected": None,
            "preview": None,
            "top_candidates": [],
            "near_miss_candidates": near_miss_candidates,
            "raw_ranked_count": len(ranked),
            "distinct_ranked_count": len(distinct_ranked),
            "capped_ranked_count": 0,
            "candidate_clusters": clusters,
            "spot_snapshot": spot_snapshot,
            "catalyst_snapshot": catalyst_snapshot,
        }

    selected = capped_ranked[0]
    client = build_clob_client(env, include_api_creds=True)
    executor = OrderExecutor(client)
    preview = executor.preview_limit_buy(
        selected.market,
        side=selected.selected_side,
        budget_usdc=budget_usdc,
        limit_price=price,
    )

    return {
        "ranked": ranked,
        "selected": selected,
        "preview": preview,
        "top_candidates": [_ranked_market_to_dict(item) for item in capped_ranked[: max(1, top)]],
        "near_miss_candidates": near_miss_candidates,
        "raw_ranked_count": len(ranked),
        "distinct_ranked_count": len(distinct_ranked),
        "capped_ranked_count": len(capped_ranked),
        "candidate_clusters": clusters,
        "spot_snapshot": spot_snapshot,
        "catalyst_snapshot": catalyst_snapshot,
    }


def _ranked_market_to_dict(item: RankedMarket) -> dict[str, object]:
    return {
        "market_id": item.market.market_id,
        "slug": item.market.slug,
        "question": item.market.question,
        "score": item.score,
        "selected_side": item.selected_side.value,
        "selected_price": item.selected_price,
        "hours_to_close": item.hours_to_close,
        "volume": item.market.volume,
        "category": item.market.category,
    }


def _near_miss_candidates(
    decisions,
    ranker: EdgeRanker,
    *,
    top: int,
    spot_snapshot=None,
    catalyst_snapshot=None,
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for decision in decisions:
        if decision.qualifies:
            continue
        score = ranker.score_market(
            decision.market,
            spot_snapshot=spot_snapshot,
            catalyst_snapshot=catalyst_snapshot,
        )
        candidates.append(
            {
                "market_id": decision.market.market_id,
                "slug": decision.market.slug,
                "question": decision.market.question,
                "score_if_scored": score,
                "selected_side": decision.market.near_certain_side.value,
                "selected_price": decision.market.near_certain_price,
                "hours_to_close": round(decision.hours_to_close, 4),
                "volume": decision.market.volume,
                "category": decision.market.category,
                "failed_rules": list(decision.reasons),
            }
        )
    candidates.sort(key=lambda item: (len(item["failed_rules"]), -item["score_if_scored"]))
    return candidates[:top]
if __name__ == "__main__":
    main()
