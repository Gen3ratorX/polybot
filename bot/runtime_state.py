from __future__ import annotations

import asyncio
import time
from threading import Thread
from dataclasses import dataclass

from api.clob import get_collateral_status
from bot.alerts import AlertDelivery, TelegramAlerter
from bot.autoscale import AutoScaleEngine
from bot.config import EnvironmentConfig, RuntimeConfig
from bot.tracker import TradeTracker
from models import BotState


@dataclass(frozen=True, slots=True)
class StateSyncResult:
    state: BotState
    state_id: int
    bankroll: float


_RECENT_ALERTS: dict[tuple[str, str], float] = {}


def sync_live_state(
    *,
    tracker: TradeTracker,
    runtime: RuntimeConfig,
    env: EnvironmentConfig,
    clob_client,
    open_orders: int | None = None,
    open_positions: int | None = None,
    is_paused: bool = False,
    pause_level: str | None = None,
    pause_reason: str | None = None,
    pause_until=None,
) -> StateSyncResult:
    collateral = get_collateral_status(clob_client, env)
    autoscale = AutoScaleEngine()
    phase = autoscale.get_phase_info(collateral.balance_usdc).phase
    state = tracker.build_state_snapshot(
        bankroll=collateral.balance_usdc,
        phase=phase,
        strategy_min_price=runtime.strategy.min_price,
        strategy_min_score=runtime.strategy.min_score,
        strategy_name=runtime.strategy.name,
        open_orders=open_orders,
        open_positions=open_positions,
        is_paused=is_paused,
        pause_level=pause_level,
        pause_reason=pause_reason,
        pause_until=pause_until,
    )
    state_id = tracker.record_state(state)
    return StateSyncResult(
        state=state,
        state_id=state_id,
        bankroll=collateral.balance_usdc,
    )


def send_optional_alert(
    env: EnvironmentConfig,
    message: str,
    *,
    level: str = "INFO",
    dedupe_window_seconds: float = 300.0,
) -> AlertDelivery | None:
    if not env.telegram_token or not env.telegram_chat_id:
        return None
    fingerprint = (level, message)
    now = time.monotonic()
    previous = _RECENT_ALERTS.get(fingerprint)
    if previous is not None and now - previous <= dedupe_window_seconds:
        return AlertDelivery(ok=True, status=208, response={"ok": True, "deduped": True})
    _RECENT_ALERTS[fingerprint] = now
    cutoff = now - max(0.0, dedupe_window_seconds)
    stale = [key for key, seen_at in _RECENT_ALERTS.items() if seen_at < cutoff]
    for key in stale:
        _RECENT_ALERTS.pop(key, None)
    alerter = TelegramAlerter(env.telegram_token, env.telegram_chat_id)
    return _run_alert_coroutine(alerter.send_alert(message, level=level))


def _run_alert_coroutine(alert_coroutine):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(alert_coroutine)

    result: list[AlertDelivery] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(alert_coroutine))
        except BaseException as exc:  # pragma: no cover - defensive propagation
            error.append(exc)

    thread = Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    if not result:
        raise RuntimeError("alert delivery did not complete")
    return result[0]
