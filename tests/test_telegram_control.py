from __future__ import annotations

import pytest

from api.telegram import TelegramMessage
from bot.telegram_control import TelegramCommandRouter, parse_command
from bot.tracker import TradeTracker


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str, **_: object) -> dict[str, object]:
        self.sent.append((chat_id, text))
        return {"ok": True}


def _message(text: str) -> TelegramMessage:
    return TelegramMessage(
        message_id=1,
        chat_id="chat",
        text=text,
        from_user_id=123,
        username="tester",
        raw={"text": text},
    )


def test_parse_command_supports_profile_and_global_commands() -> None:
    assert parse_command(_message("/status")) == ("status", None)
    assert parse_command(_message("/pause late_market_edge")) == ("pause", "late_market_edge")
    assert parse_command(_message("/help")) == ("help", None)
    assert parse_command(_message("/go_live")) == ("go_live", None)
    assert parse_command(_message("hello")) == (None, None)


@pytest.mark.asyncio
async def test_router_updates_profile_and_global_controls(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'router.db'}")
    tracker.initialize()
    env = type(
        "Env",
        (),
        {"telegram_token": "token", "telegram_chat_id": "chat", "database_url": tracker.database_url},
    )()
    router = TelegramCommandRouter(env=env, tracker=tracker)
    client = FakeClient()

    pause_result = await router.route_command(
        client,
        command="pause",
        profile_name="late_market_edge",
        message=_message("/pause late_market_edge"),
    )
    help_result = await router.route_command(
        client,
        command="help",
        profile_name=None,
        message=_message("/help"),
    )
    run_once_result = await router.route_command(
        client,
        command="run_once",
        profile_name="late_market_edge",
        message=_message("/run_once late_market_edge"),
    )
    go_live_result = await router.route_command(
        client,
        command="go_live",
        profile_name=None,
        message=_message("/go_live"),
    )

    profile_control_before_stop = tracker.get_control_state("late_market_edge")
    submit_enabled = tracker.get_runtime_setting("submit_enabled")

    global_stop_result = await router.route_command(
        client,
        command="stop_all",
        profile_name=None,
        message=_message("/stop_all"),
    )

    profile_control = tracker.get_control_state("late_market_edge")
    global_control = tracker.get_control_state(None)

    assert pause_result.handled is True
    assert help_result.handled is True
    assert run_once_result.handled is True
    assert go_live_result.handled is True
    assert profile_control is not None
    assert profile_control_before_stop is not None
    assert profile_control_before_stop.desired_state == "PAUSED"
    assert profile_control_before_stop.run_once_pending == 1
    assert submit_enabled is not None
    assert submit_enabled["setting_value"] == "true"

    assert global_stop_result.handled is True
    assert global_control is not None
    assert global_control.desired_state == "STOPPED"
    assert client.sent
    assert any("Polybot Telegram commands:" in text for _, text in client.sent)
    assert any("Profile status: late_market_edge" in text for _, text in client.sent)
    assert any("Global control status:" in text for _, text in client.sent)
