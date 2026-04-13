from __future__ import annotations

from bot.config import load_environment, load_runtime_config
from bot.runtime_state import send_optional_alert, sync_live_state
from bot.tracker import TradeTracker


class DummyClient:
    pass


def test_sync_live_state_builds_and_records_snapshot(tmp_path, monkeypatch) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'runtime.db'}")
    tracker.initialize()
    runtime = load_runtime_config("config.yaml")
    env = load_environment()

    class Status:
        balance_usdc = 12.5

    monkeypatch.setattr("bot.runtime_state.get_collateral_status", lambda client, env: Status())

    result = sync_live_state(
        tracker=tracker,
        runtime=runtime,
        env=env,
        clob_client=DummyClient(),
    )

    assert result.bankroll == 12.5
    assert result.state_id == 1
    assert result.state.bankroll == 12.5
    assert result.state.strategy_name == runtime.strategy.name
    assert tracker.get_latest_state() is not None


def test_send_optional_alert_suppresses_exact_duplicates(monkeypatch) -> None:
    deliveries = []

    class FakeAlerter:
        def __init__(self, token, chat_id):
            self.token = token
            self.chat_id = chat_id

        async def send_alert(self, message, level="INFO"):
            deliveries.append((level, message))
            return type("Delivery", (), {"ok": True, "status": 200, "response": {"ok": True}})()

    env = type(
        "Env",
        (),
        {"telegram_token": "token", "telegram_chat_id": "chat"},
    )()

    monkeypatch.setattr("bot.runtime_state.TelegramAlerter", FakeAlerter)
    monkeypatch.setattr("bot.runtime_state._RECENT_ALERTS", {})

    first = send_optional_alert(env, "hello", level="INFO", dedupe_window_seconds=300.0)
    second = send_optional_alert(env, "hello", level="INFO", dedupe_window_seconds=300.0)

    assert first is not None
    assert second is not None
    assert first.status == 200
    assert second.status == 208
    assert deliveries == [("INFO", "hello")]
