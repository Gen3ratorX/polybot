from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from models.trade import Trade


@dataclass(frozen=True, slots=True)
class BotState:
    timestamp: datetime
    bankroll: float
    phase: int
    bankroll_start_of_day: float
    bankroll_start_of_week: float
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    total_trades: int = 0
    consecutive_failures: int = 0
    open_orders: int = 0
    open_positions: int = 0
    strategy_min_price: float = 0.85
    strategy_min_score: float = 7.0
    is_paused: bool = False
    pause_level: str | None = None
    pause_reason: str | None = None
    pause_until: datetime | None = None
    recent_trades: tuple[Trade, ...] = field(default_factory=tuple)
    strategy_name: str | None = None

    def __post_init__(self) -> None:
        _require_aware_datetime(self.timestamp, "timestamp")
        if self.bankroll < 0:
            raise ValueError("bankroll must be non-negative")
        if self.phase < 0:
            raise ValueError("phase must be non-negative")
        if self.bankroll_start_of_day < 0 or self.bankroll_start_of_week < 0:
            raise ValueError("bankroll baselines must be non-negative")
        if self.total_trades < 0:
            raise ValueError("total_trades must be non-negative")
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures must be non-negative")
        if self.open_orders < 0 or self.open_positions < 0:
            raise ValueError("open order and position counts must be non-negative")
        if not 0 <= self.strategy_min_price <= 1:
            raise ValueError("strategy_min_price must be between 0 and 1")
        if not 0 <= self.strategy_min_score <= 10:
            raise ValueError("strategy_min_score must be between 0 and 10")
        if self.pause_until is not None:
            _require_aware_datetime(self.pause_until, "pause_until")
        if self.strategy_name is not None and not self.strategy_name.strip():
            raise ValueError("strategy_name must not be empty when provided")
        if not isinstance(self.recent_trades, tuple):
            raise ValueError("recent_trades must be stored as a tuple")

    @property
    def daily_loss(self) -> float:
        return max(0.0, -self.daily_pnl)

    @property
    def weekly_loss(self) -> float:
        return max(0.0, -self.weekly_pnl)

    def get_recent_trades(self, limit: int) -> tuple[Trade, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        return self.recent_trades[-limit:]

    def win_rate(self, limit: int) -> float:
        trades = self.get_recent_trades(limit)
        resolved = [trade for trade in trades if trade.pnl is not None]
        if not resolved:
            return 0.0
        wins = [trade for trade in resolved if trade.pnl > 0]
        return len(wins) / len(resolved)


def _require_aware_datetime(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware")
