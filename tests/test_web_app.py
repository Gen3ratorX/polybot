from __future__ import annotations

from datetime import UTC, datetime

from bot.web_app import _apply_control, _build_status_payload, _is_authorized
from bot.control_service import ControlService
from bot.tracker import TradeTracker
from models import BotState, OutcomeSide, Trade, TradeOutcome


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

    trade = Trade(
        timestamp=datetime.now(UTC),
        strategy_name="late_market_edge",
        market_id="m1",
        market_question="Question",
        category="sports",
        side=OutcomeSide.YES,
        entry_price=0.9,
        position_size=9.0,
        outcome=TradeOutcome.CANCELLED,
        order_id="oid",
        paper_trade=False,
    )
    tracker.record_trade(trade)
    tracker.record_state(
        BotState(
            timestamp=datetime.now(UTC),
            strategy_name="late_market_edge",
            bankroll=10.0,
            phase=1,
            bankroll_start_of_day=10.0,
            bankroll_start_of_week=10.0,
            total_trades=1,
            open_positions=0,
            recent_trades=(trade,),
        )
    )

    payload = _build_status_payload(service, tracker)
    assert payload["global_control"]["desired_state"] == "RUNNING"
    assert payload["profiles"]
    assert payload["latest_state"] is not None
    assert payload["latest_state"]["bankroll"] == 10.0
    assert "svg" in payload["global_curve_svg"]

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
    assert any(profile["equity_curve_svg"] is not None for profile in control_payload["profiles"])


def test_web_app_basic_auth_parser() -> None:
    assert _is_authorized(
        "Basic dXNlcjpwYXNz",
        "user:pass",
    )
    assert not _is_authorized("Basic bad-token", "user:pass")
    assert not _is_authorized(None, "user:pass")
