from __future__ import annotations

from api.spot import SpotFeedClient, SpotSnapshot
from bot.config import EnvironmentConfig, RuntimeConfig
from bot.runtime_state import send_optional_alert
from bot.tracker import TradeTracker


async def load_active_spot_snapshot(
    *,
    env: EnvironmentConfig | None,
    runtime: RuntimeConfig,
    tracker: TradeTracker | None = None,
) -> SpotSnapshot | None:
    if runtime.strategy.signal_mode != "momentum":
        return None

    cached = tracker.get_latest_spot_snapshot(runtime.strategy.spot_symbol) if tracker is not None else None
    try:
        async with SpotFeedClient() as spot_client:
            snapshot = await spot_client.fetch_snapshot(runtime.strategy.spot_symbol)
    except Exception as exc:
        if cached is not None:
            return cached
        if env is not None:
            send_optional_alert(
                env,
                f"BTC spot feed unavailable for {runtime.strategy.spot_symbol}: {exc}",
                level="WARN",
            )
        return None

    if tracker is not None:
        tracker.record_spot_tick(snapshot)
    return snapshot
