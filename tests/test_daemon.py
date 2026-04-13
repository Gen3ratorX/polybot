from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from bot.config import load_runtime_config
from bot.daemon import compute_daemon_sleep_seconds, run_daemon
from bot.supervisor import ExecutedCycle, PreparedCycle
from bot.tracker import TradeTracker


@dataclass(frozen=True, slots=True)
class DummyEnv:
    database_url: str


def test_compute_daemon_sleep_seconds_uses_latest_bankroll_when_no_override(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'daemon.db'}")
    tracker.initialize()
    tracker.record_state(
        tracker.build_state_snapshot(
            bankroll=100.0,
            phase=1,
            strategy_min_price=0.85,
            strategy_min_score=7.0,
        )
    )

    sleep_seconds = compute_daemon_sleep_seconds(tracker)

    assert sleep_seconds == 90.0


def test_run_daemon_executes_bounded_cycles_and_uses_override_sleep(tmp_path, monkeypatch) -> None:
    env = DummyEnv(database_url=f"sqlite:///{tmp_path / 'daemon-run.db'}")
    runtime = load_runtime_config("config.yaml")
    sleeps: list[float] = []
    alerts: list[tuple[str, str]] = []
    calls: list[int] = []

    monkeypatch.setattr("bot.daemon.send_optional_alert", lambda env, message, level='INFO': alerts.append((level, message)))

    def fake_cycle_runner(**kwargs):
        calls.append(1)
        return ExecutedCycle(
            prepared=PreparedCycle(
                state_id=len(calls),
                bankroll=10.0,
                phase=0,
                budget_cap=0.0,
                selected_budget=0.0,
                kill_signal=None,
                skip_reason="skip",
                reconciled_positions=(),
                top_candidates=(),
                near_miss_candidates=(),
                preview=None,
            ),
            submit_response=None,
            cancel_response=None,
            final_order_status=None,
            tracked_trade_id=None,
            position_id=None,
            trade_outcome=None,
            state_id=None,
        )

    results = run_daemon(
        env=env,  # type: ignore[arg-type]
        runtime=runtime,
        cycles=2,
        requested_budget=None,
        limit_price=None,
        top=5,
        near_misses=5,
        submit=False,
        monitor_seconds=20,
        poll_interval=2.0,
        cancel_if_open=False,
        sleep_override_seconds=1.5,
        sleeper=sleeps.append,
        cycle_runner=fake_cycle_runner,
    )

    assert len(results) == 2
    assert sleeps == [1.5]
    assert len(calls) == 2
    assert len(alerts) == 2
