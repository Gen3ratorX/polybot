from __future__ import annotations

import argparse
import json
from datetime import UTC

from bot.autoscale import AutoScaleEngine
from bot.config import load_environment, load_runtime_config
from bot.catalyst import load_cached_catalyst_snapshot
from bot.tracker import TradeTracker


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket bot status entrypoint.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Render status as JSON instead of line-oriented text",
    )
    parser.add_argument(
        "--strategy-section",
        default="late_market_edge",
        help="Which strategy profile to report. Defaults to late_market_edge.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runtime = load_runtime_config(strategy_section=args.strategy_section)
    env = load_environment()
    tracker = TradeTracker(env.database_url)
    tracker.initialize()
    latest_state = tracker.get_latest_state(strategy_name=runtime.strategy.name) or tracker.get_latest_state()
    latest_spot = _load_latest_spot_snapshot(tracker, runtime)
    latest_catalyst = _load_latest_catalyst_snapshot(tracker, runtime)
    profile_performance = tracker.profile_performance_report()
    autoscale = AutoScaleEngine()
    bankroll = latest_state.bankroll if latest_state is not None else 0.0
    active_profile = runtime.strategy.name
    active_trade_count = tracker.trade_count(strategy_name=active_profile)
    active_open_position_count = tracker.open_position_count(strategy_name=active_profile)
    active_open_order_count = tracker.open_order_count(strategy_name=active_profile)
    phase = autoscale.get_phase_info(bankroll)
    payload = {
        "paper_trade": env.paper_trade,
        "database_url": env.database_url,
        "state_strategy_name": latest_state.strategy_name if latest_state is not None else None,
        "trade_count": active_trade_count,
        "global_trade_count": tracker.trade_count(),
        "open_position_count": active_open_position_count,
        "global_open_position_count": tracker.open_position_count(),
        "open_order_count": active_open_order_count,
        "global_open_order_count": tracker.open_order_count(),
        "bankroll": bankroll,
        "phase": phase.phase,
        "phase_label": phase.label,
        "phase_mode": phase.mode,
        "scan_interval_seconds": autoscale.get_scan_interval(bankroll),
        "max_position_size": autoscale.get_max_position_size(bankroll),
        "profile_performance": profile_performance,
        "spot": _spot_payload(latest_spot),
        "catalyst": _catalyst_payload(latest_catalyst),
        "strategy": {
            "name": runtime.strategy.name,
            "signal_mode": runtime.strategy.signal_mode,
            "execution_style": runtime.strategy.execution_style or runtime.execution.execution_style,
            "spot_symbol": runtime.strategy.spot_symbol if runtime.strategy.signal_mode == "momentum" else None,
            "spot_symbols": list(runtime.strategy.spot_symbols) if runtime.strategy.spot_symbols else None,
            "min_price": runtime.strategy.min_price,
            "max_price": runtime.strategy.max_price,
            "min_score": runtime.strategy.min_score,
            "window_hours": [
                runtime.strategy.min_hours_to_close,
                runtime.strategy.max_hours_to_close,
            ],
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return

    print("Polymarket Edge Bot")
    print(f"paper_trade={payload['paper_trade']}")
    print(f"database_url={payload['database_url']}")
    print(f"state_strategy_name={payload['state_strategy_name']}")
    print(f"trade_count={payload['trade_count']}")
    print(f"global_trade_count={payload['global_trade_count']}")
    print(f"open_position_count={payload['open_position_count']}")
    print(f"global_open_position_count={payload['global_open_position_count']}")
    print(f"open_order_count={payload['open_order_count']}")
    print(f"global_open_order_count={payload['global_open_order_count']}")
    print(f"bankroll={payload['bankroll']}")
    print(f"phase={payload['phase']} ({payload['phase_label']} / {payload['phase_mode']})")
    print(f"scan_interval_seconds={payload['scan_interval_seconds']}")
    print(f"max_position_size={payload['max_position_size']}")
    print(f"strategy.name={runtime.strategy.name}")
    print(f"strategy.signal_mode={runtime.strategy.signal_mode}")
    print(f"strategy.execution_style={runtime.strategy.execution_style or runtime.execution.execution_style}")
    for profile in profile_performance:
        print(
            "profile="
            f"{profile['strategy_name']} "
            f"trades={profile['trade_count']} "
            f"open_positions={profile['open_positions']} "
            f"open_orders={profile['open_orders']} "
            f"win_rate={_format_profile_win_rate(profile['win_rate'])} "
            f"pnl=${profile['total_pnl']:.2f}"
        )
    if runtime.strategy.signal_mode == "momentum":
        print(f"strategy.spot_symbol={runtime.strategy.spot_symbol}")
        if runtime.strategy.spot_symbols:
            print(f"strategy.spot_symbols={','.join(runtime.strategy.spot_symbols)}")
        print(_spot_line(latest_spot))
        print(_catalyst_line(latest_catalyst))
    print(f"strategy.min_price={runtime.strategy.min_price}")
    print(f"strategy.max_price={runtime.strategy.max_price}")
    print(f"strategy.min_score={runtime.strategy.min_score}")


def _load_latest_spot_snapshot(tracker: TradeTracker, runtime):
    if runtime.strategy.signal_mode != "momentum":
        return None
    if runtime.strategy.spot_symbols:
        snapshots = {
            symbol: tracker.get_latest_spot_snapshot(symbol)
            for symbol in runtime.strategy.spot_symbols
        }
        return {symbol: snapshot for symbol, snapshot in snapshots.items() if snapshot is not None} or None
    return tracker.get_latest_spot_snapshot(runtime.strategy.spot_symbol)


def _load_latest_catalyst_snapshot(tracker: TradeTracker, runtime):
    if runtime.strategy.signal_mode != "momentum":
        return None
    return load_cached_catalyst_snapshot(tracker=tracker, runtime=runtime)


def _spot_payload(snapshot) -> dict[str, object] | None:
    if snapshot is None:
        return None
    if isinstance(snapshot, dict):
        return {symbol: item.as_dict() for symbol, item in snapshot.items()}
    return snapshot.as_dict()


def _catalyst_payload(snapshot) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return snapshot.as_dict()


def _spot_line(snapshot) -> str:
    if snapshot is None:
        return "spot_snapshot=none"
    if isinstance(snapshot, dict):
        parts = [
            f"{symbol}:price={item.spot_price:.2f} ret_1h={_format_pct(item.return_1h_pct)} age_seconds={item.age_seconds:.0f}"
            for symbol, item in snapshot.items()
        ]
        return "spot_snapshot=" + " | ".join(parts)
    return (
        "spot_snapshot="
        f"price={snapshot.spot_price:.2f} "
        f"fetch_latency_seconds={_format_seconds(snapshot.fetch_latency_seconds)} "
        f"ret_15m={_format_pct(snapshot.return_15m_pct)} "
        f"ret_1h={_format_pct(snapshot.return_1h_pct)} "
        f"age_seconds={snapshot.age_seconds:.0f} "
        f"observed_at={snapshot.observed_at.astimezone(UTC).isoformat()}"
    )


def _catalyst_line(snapshot) -> str:
    if snapshot is None:
        return "catalyst_snapshot=none"
    active_count = len(snapshot.active_events)
    return (
        "catalyst_snapshot="
        f"provider={snapshot.provider} "
        f"events={len(snapshot.events)} "
        f"active_events={active_count} "
        f"fetch_latency_seconds={_format_seconds(snapshot.fetch_latency_seconds)}"
    )


def _format_pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3%}"


def _format_seconds(value) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}s"


def _format_profile_win_rate(value) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}"


if __name__ == "__main__":
    main()
