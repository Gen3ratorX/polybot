from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from bot.config import AutoCorrectConfig, StrategyConfig
from models import Trade


@dataclass(frozen=True, slots=True)
class CorrectionResult:
    trigger_win_rate: float
    action: str
    adjustments: dict[str, float | bool | str]


class AutoCorrectEngine:
    def __init__(
        self,
        strategy_config: StrategyConfig,
        autocorrect_config: AutoCorrectConfig,
    ) -> None:
        self.strategy_config = strategy_config
        self.autocorrect_config = autocorrect_config

    def run_correction_cycle(self, last_50_trades: list[Trade]) -> CorrectionResult:
        if len(last_50_trades) != self.autocorrect_config.cycle_trades:
            raise ValueError("Correction cycle requires exactly 50 trades")

        resolved = [trade for trade in last_50_trades if trade.pnl is not None]
        if len(resolved) != len(last_50_trades):
            raise ValueError("All trades in correction cycle must be resolved")

        wins = [trade for trade in resolved if (trade.pnl or 0) > 0]
        win_rate = len(wins) / len(resolved)
        adjustments: dict[str, float | bool | str] = {}

        if win_rate > self.autocorrect_config.loosen_threshold:
            adjustments["min_price"] = round(
                max(0.83, self.strategy_config.min_price - 0.02),
                2,
            )
            action = "LOOSEN"
        elif win_rate >= 0.88:
            action = "HOLD"
        elif win_rate >= self.autocorrect_config.tighten_threshold:
            adjustments["min_price"] = round(
                min(0.90, self.strategy_config.min_price + 0.02),
                2,
            )
            adjustments["min_score"] = 7.5
            action = "TIGHTEN"
        elif win_rate >= self.autocorrect_config.warn_threshold:
            adjustments["min_price"] = 0.92
            adjustments["min_score"] = 8.0
            action = "WARN"
        else:
            action = "PAUSE"

        by_category: dict[str, list[Trade]] = defaultdict(list)
        for trade in resolved:
            by_category[(trade.category or "unknown").lower()].append(trade)

        for category, trades in by_category.items():
            if not trades:
                continue
            category_win_rate = sum(1 for trade in trades if (trade.pnl or 0) > 0) / len(trades)
            if category_win_rate < 0.75:
                adjustments[f"disable_category_{category}"] = True

        return CorrectionResult(
            trigger_win_rate=win_rate,
            action=action,
            adjustments=adjustments,
        )
