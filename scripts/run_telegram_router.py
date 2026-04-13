from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_environment
from bot.telegram_control import TelegramCommandRouter
from bot.tracker import TradeTracker


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Telegram command router.")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Seconds between Telegram getUpdates polls.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env = load_environment()
    tracker = TradeTracker(env.database_url)
    tracker.initialize()
    router = TelegramCommandRouter(env=env, tracker=tracker)
    try:
        asyncio.run(router.run(poll_interval_seconds=args.poll_interval))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
