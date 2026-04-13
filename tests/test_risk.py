from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.risk import RiskManager
from models import BotState, OutcomeSide, Trade, TradeOutcome


def test_risk_manager_triggers_l6_error_stop_first() -> None:
    manager = RiskManager()
    state = _state(consecutive_failures=3)

    signal = manager.check_all_kills(state)

    assert signal is not None
    assert signal.level == "L6"
    assert signal.pause_minutes == 30


def test_risk_manager_triggers_l5_balance_stop() -> None:
    manager = RiskManager()
    state = _state(bankroll=4.99, phase=1)

    signal = manager.check_all_kills(state)

    assert signal is not None
    assert signal.level == "L5"
    assert signal.manual_resume_required is True


def test_risk_manager_triggers_l2_hard_daily_loss() -> None:
    manager = RiskManager()
    state = _state(
        bankroll=80.0,
        bankroll_start_of_day=100.0,
        daily_pnl=-16.0,
    )

    signal = manager.check_all_kills(state)

    assert signal is not None
    assert signal.level == "L2"
    assert signal.pause_minutes == 1440


def test_risk_manager_triggers_l1_soft_daily_loss() -> None:
    manager = RiskManager()
    state = _state(
        bankroll=89.0,
        bankroll_start_of_day=100.0,
        daily_pnl=-11.0,
    )

    signal = manager.check_all_kills(state)

    assert signal is not None
    assert signal.level == "L1"
    assert signal.pause_minutes == 360


def test_risk_manager_triggers_l3_weekly_stop() -> None:
    manager = RiskManager()
    state = _state(
        bankroll=70.0,
        bankroll_start_of_day=100.0,
        bankroll_start_of_week=100.0,
        daily_pnl=-5.0,
        weekly_pnl=-30.0,
    )

    signal = manager.check_all_kills(state)

    assert signal is not None
    assert signal.level == "L3"
    assert signal.manual_resume_required is True


def test_risk_manager_triggers_l4_win_rate_stop() -> None:
    manager = RiskManager()
    recent_trades = tuple(
        _resolved_trade(f"trade-{index}", pnl=1.0 if index < 69 else -1.0)
        for index in range(100)
    )
    state = _state(
        total_trades=100,
        recent_trades=recent_trades,
    )

    signal = manager.check_all_kills(state)

    assert signal is not None
    assert signal.level == "L4"
    assert signal.manual_resume_required is True


def test_risk_manager_returns_none_when_clear() -> None:
    manager = RiskManager()
    recent_trades = tuple(
        _resolved_trade(f"trade-{index}", pnl=1.0 if index < 90 else -1.0)
        for index in range(100)
    )
    state = _state(
        bankroll=50.0,
        phase=2,
        total_trades=100,
        recent_trades=recent_trades,
    )

    signal = manager.check_all_kills(state)

    assert signal is None


def _state(
    *,
    bankroll: float = 20.0,
    phase: int = 0,
    bankroll_start_of_day: float = 20.0,
    bankroll_start_of_week: float = 20.0,
    daily_pnl: float = 0.0,
    weekly_pnl: float = 0.0,
    total_trades: int = 0,
    consecutive_failures: int = 0,
    recent_trades: tuple[Trade, ...] = (),
) -> BotState:
    return BotState(
        timestamp=datetime.now(UTC),
        bankroll=bankroll,
        phase=phase,
        bankroll_start_of_day=bankroll_start_of_day,
        bankroll_start_of_week=bankroll_start_of_week,
        daily_pnl=daily_pnl,
        weekly_pnl=weekly_pnl,
        total_trades=total_trades,
        consecutive_failures=consecutive_failures,
        recent_trades=recent_trades,
    )


def _resolved_trade(market_id: str, *, pnl: float) -> Trade:
    return Trade(
        timestamp=datetime.now(UTC) - timedelta(minutes=1),
        market_id=market_id,
        market_question=f"Question {market_id}",
        side=OutcomeSide.YES,
        entry_price=0.9,
        position_size=9.0,
        outcome=TradeOutcome.WIN if pnl > 0 else TradeOutcome.LOSS,
        resolution_price=1.0 if pnl > 0 else 0.0,
        pnl=pnl,
    )
