from __future__ import annotations

from bot.web_app import _apply_control, _build_status_payload, _is_authorized
from bot.control_service import ControlService
from bot.tracker import TradeTracker


def test_web_app_status_payload_and_control(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'web.db'}")
    tracker.initialize()
    env = type(
        "Env",
        (),
        {
            "database_url": tracker.database_url,
            "web_username": None,
            "web_password": None,
        },
    )()
    service = ControlService(tracker, tracker.database_url)

    payload = _build_status_payload(service, tracker)
    assert payload["global_control"]["desired_state"] == "RUNNING"
    assert payload["profiles"]
    assert payload["latest_state"] is None

    result = _apply_control(
        service,
        {"command": "pause", "profile_name": "late_market_edge", "actor": "test"},
    )
    assert result["handled"] is True
    assert result["scope"] == "PROFILE"

    profile_state = tracker.get_control_state("late_market_edge")
    assert profile_state is not None
    assert profile_state.desired_state == "PAUSED"

    control_payload = _build_status_payload(service, tracker)
    assert control_payload["profiles"]


def test_web_app_basic_auth_parser() -> None:
    assert _is_authorized(
        "Basic dXNlcjpwYXNz",
        "user:pass",
    )
    assert not _is_authorized("Basic bad-token", "user:pass")
    assert not _is_authorized(None, "user:pass")

