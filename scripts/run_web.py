from __future__ import annotations

import sys
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_environment
from bot.tracker import TradeTracker
from bot.web_app import create_web_app


def main() -> None:
    env = load_environment()
    tracker = TradeTracker(env.database_url)
    tracker.initialize()
    app = create_web_app(env=env, tracker=tracker)
    web.run_app(app, host=env.web_bind_host, port=env.web_bind_port)


if __name__ == "__main__":
    main()
