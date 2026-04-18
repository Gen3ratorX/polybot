from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_environment, load_runtime_config
from bot.daemon import run_daemon
from bot.logging_utils import configure_cli_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the long-lived supervised Polybot daemon."
    )
    parser.add_argument(
        "--cycles",
        type=int,
        default=0,
        help="How many cycles to run. Use 0 for an infinite daemon loop.",
    )
    parser.add_argument("--budget-usdc", type=float, default=None)
    parser.add_argument("--price", type=float, default=None)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--near-misses", type=int, default=5)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--monitor-seconds", type=int, default=20)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--cancel-if-open", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=None)
    parser.add_argument(
        "--debug-http",
        action="store_true",
        help="Enable DEBUG logging for Gamma/spot/catalyst HTTP requests",
    )
    parser.add_argument(
        "--strategy-section",
        default="late_market_edge",
        help="Which strategy profile to use. Defaults to late_market_edge.",
    )
    return parser.parse_args()


def main() -> None:
    try:
        args = parse_args()
        configure_cli_logging(debug_http=args.debug_http)
        env = load_environment()
        runtime = load_runtime_config(strategy_section=args.strategy_section)
        results = run_daemon(
            env=env,
            runtime=runtime,
            cycles=None if args.cycles == 0 else args.cycles,
            requested_budget=args.budget_usdc,
            limit_price=args.price,
            top=args.top,
            near_misses=args.near_misses,
            submit=args.submit,
            monitor_seconds=args.monitor_seconds,
            poll_interval=args.poll_interval,
            cancel_if_open=args.cancel_if_open,
            sleep_override_seconds=args.sleep_seconds,
        )
        print(json.dumps({"cycle_count": len(results)}, indent=2))
        print(json.dumps({"cycles": [item.as_dict() for item in results]}, indent=2))
    except KeyboardInterrupt:
        print("daemon_stopped: keyboard interrupt", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"run_daemon_error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
