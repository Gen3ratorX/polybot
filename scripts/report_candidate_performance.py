from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_environment
from bot.tracker import TradeTracker


def main() -> None:
    env = load_environment()
    tracker = TradeTracker(env.database_url)
    tracker.initialize()
    print(json.dumps(tracker.candidate_performance_report(), indent=2))


if __name__ == "__main__":
    main()
