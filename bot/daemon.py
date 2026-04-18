from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from bot.autoscale import AutoScaleEngine
from bot.config import EnvironmentConfig, RuntimeConfig
from bot.runtime_state import send_optional_alert
from bot.supervisor import ExecutedCycle, TradeQuotaState, run_supervised_cycle
from bot.tracker import TradeTracker


SUBMIT_ENABLED_SETTING_KEY = "submit_enabled"


@dataclass(frozen=True, slots=True)
class DaemonCycle:
    cycle_index: int
    sleep_seconds: float
    executed: ExecutedCycle

    def as_dict(self) -> dict[str, object]:
        return {
            "cycle_index": self.cycle_index,
            "sleep_seconds": self.sleep_seconds,
            "executed": self.executed.as_dict(),
        }


def compute_daemon_sleep_seconds(
    tracker: TradeTracker,
    *,
    override_seconds: float | None = None,
) -> float:
    if override_seconds is not None:
        return max(0.0, override_seconds)
    latest_state = tracker.get_latest_state()
    bankroll = latest_state.bankroll if latest_state is not None else 0.0
    return float(AutoScaleEngine().get_scan_interval(bankroll))


def run_daemon(
    *,
    env: EnvironmentConfig,
    runtime: RuntimeConfig,
    cycles: int | None,
    requested_budget: float | None,
    limit_price: float | None,
    top: int,
    near_misses: int,
    submit: bool,
    monitor_seconds: int,
    poll_interval: float,
    cancel_if_open: bool,
    sleep_override_seconds: float | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    cycle_runner: Callable[..., ExecutedCycle] = run_supervised_cycle,
) -> list[DaemonCycle]:
    if cycles is not None and cycles <= 0:
        raise ValueError("cycles must be positive when provided")

    tracker = TradeTracker(env.database_url)
    tracker.initialize()
    quota_state = (
        TradeQuotaState(target_trades=runtime.strategy.trade_quota_target_trades or 0)
        if runtime.strategy.trade_quota_enabled
        else None
    )
    paper_only = bool(env.paper_trade)
    if tracker.get_runtime_setting(SUBMIT_ENABLED_SETTING_KEY) is None:
        tracker.upsert_runtime_setting(
            SUBMIT_ENABLED_SETTING_KEY,
            "false" if paper_only else ("true" if submit else "false"),
            updated_by="daemon",
            last_command="/daemon_start",
            notes="Daemon startup submit default" if not paper_only else "Paper mode forces submit disabled",
        )
    results: list[DaemonCycle] = []
    send_optional_alert(
        env,
        (
            "Polybot daemon started\n"
            f"submit={submit}\n"
            f"cycles={'infinite' if cycles is None else cycles}"
        ),
        level="INFO",
    )
    completed = 0
    try:
        while cycles is None or completed < cycles:
            effective_submit = False if paper_only else _resolve_submit_enabled(tracker, default_submit=submit)
            executed = cycle_runner(
                env=env,
                runtime=runtime,
                tracker=tracker,
                requested_budget=requested_budget,
                limit_price=limit_price,
                top=top,
                near_misses=near_misses,
                submit=effective_submit,
                monitor_seconds=monitor_seconds,
                poll_interval=poll_interval,
                cancel_if_open=cancel_if_open,
                quota_state=quota_state,
            )
            sleep_seconds = compute_daemon_sleep_seconds(
                tracker,
                override_seconds=sleep_override_seconds,
            )
            results.append(
                DaemonCycle(
                    cycle_index=completed + 1,
                    sleep_seconds=sleep_seconds,
                    executed=executed,
                )
            )
            completed += 1
            if executed.execution_status == "STOPPED":
                break
            if cycles is not None and completed >= cycles:
                break
            sleeper(sleep_seconds)
    except Exception as exc:
        send_optional_alert(env, f"Polybot daemon crashed: {exc}", level="KILL")
        raise
    finally:
        send_optional_alert(
            env,
            f"Polybot daemon stopped after {completed} cycle(s).",
            level="INFO",
        )
    return results


def _resolve_submit_enabled(tracker: TradeTracker, *, default_submit: bool) -> bool:
    setting = tracker.get_runtime_setting(SUBMIT_ENABLED_SETTING_KEY)
    if setting is None:
        return default_submit
    value = str(setting["setting_value"]).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default_submit
