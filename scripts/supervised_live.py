from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.autoscale import AutoScaleEngine
from bot.config import load_environment, load_runtime_config
from bot.supervisor import run_supervised_loop


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Supervised live runner: scan, rank, risk-check, and optionally submit at most one trade per cycle."
    )
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--budget-usdc", type=float, default=None)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--near-misses", type=int, default=5)
    parser.add_argument("--sleep-seconds", type=float, default=None)
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
        autoscale = AutoScaleEngine()
        tracker_state_sleep = args.sleep_seconds
        if tracker_state_sleep is None:
            tracker_state_sleep = float(autoscale.get_scan_interval(0.0))

        results = run_supervised_loop(
            env=env,
            runtime=runtime,
            cycles=args.cycles,
            requested_budget=args.budget_usdc,
            limit_price=args.price,
            top=args.top,
            near_misses=args.near_misses,
            submit=args.submit,
            sleep_seconds=tracker_state_sleep,
            monitor_seconds=args.monitor_seconds,
            poll_interval=args.poll_interval,
            cancel_if_open=args.cancel_if_open,
        )
        print(json.dumps({"cycle_count": len(results)}, indent=2))
        print(json.dumps({"cycles": [item.as_dict() for item in results]}, indent=2))
    except Exception as exc:
        print(f"supervised_live_error: {exc}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
