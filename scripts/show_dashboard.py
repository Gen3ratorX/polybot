from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_environment
from bot.dashboard import DashboardRenderer
from bot.tracker import TradeTracker


def main() -> None:
    env = load_environment()
    tracker = TradeTracker(env.database_url)
    tracker.initialize()
    renderer = DashboardRenderer()
    snapshot = renderer.snapshot_from_tracker(tracker)
    Console().print(renderer.render(snapshot))


if __name__ == "__main__":
    main()
