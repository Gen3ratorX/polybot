from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bot.autocorrect import AutoCorrectEngine
from bot.config import load_runtime_config
from models import OutcomeSide, Trade, TradeOutcome


CONFIG = load_runtime_config("config.yaml")


def test_autocorrect_loosen_on_strong_win_rate() -> None:
    engine = AutoCorrectEngine(CONFIG.strategy, CONFIG.autocorrect)

    result = engine.run_correction_cycle(_trades(47, 3))

    assert result.action == "LOOSEN"
    assert result.adjustments["min_price"] == 0.83


def test_autocorrect_warn_and_disable_bad_category() -> None:
    engine = AutoCorrectEngine(CONFIG.strategy, CONFIG.autocorrect)
    trades = _trades(38, 12, losing_category="politics", losing_category_losses=10)

    result = engine.run_correction_cycle(trades)

    assert result.action == "WARN"
    assert result.adjustments["min_price"] == 0.92
    assert result.adjustments["min_score"] == 8.0
    assert result.adjustments["disable_category_politics"] is True


def test_autocorrect_pause_on_critical_win_rate() -> None:
    engine = AutoCorrectEngine(CONFIG.strategy, CONFIG.autocorrect)

    result = engine.run_correction_cycle(_trades(30, 20))

    assert result.action == "PAUSE"


def test_autocorrect_requires_exact_cycle_size() -> None:
    engine = AutoCorrectEngine(CONFIG.strategy, CONFIG.autocorrect)

    with pytest.raises(ValueError, match="exactly 50"):
        engine.run_correction_cycle(_trades(10, 0))


def _trades(
    wins: int,
    losses: int,
    *,
    losing_category: str = "crypto",
    losing_category_losses: int = 0,
) -> list[Trade]:
    trades: list[Trade] = []
    for index in range(wins):
        trades.append(
            Trade(
                timestamp=datetime.now(UTC),
                market_id=f"win-{index}",
                market_question=f"Win {index}",
                category="sports",
                side=OutcomeSide.YES,
                entry_price=0.9,
                position_size=9.0,
                outcome=TradeOutcome.WIN,
                resolution_price=1.0,
                pnl=1.0,
            )
        )
    for index in range(losses):
        category = losing_category if index < losing_category_losses else "sports"
        trades.append(
            Trade(
                timestamp=datetime.now(UTC),
                market_id=f"loss-{index}",
                market_question=f"Loss {index}",
                category=category,
                side=OutcomeSide.NO,
                entry_price=0.9,
                position_size=9.0,
                outcome=TradeOutcome.LOSS,
                resolution_price=0.0,
                pnl=-1.0,
            )
        )
    return trades
