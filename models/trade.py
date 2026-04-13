from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from models.enums import OutcomeSide, TradeOutcome


@dataclass(frozen=True, slots=True)
class Trade:
    timestamp: datetime
    market_id: str
    market_question: str
    side: OutcomeSide
    entry_price: float
    position_size: float
    category: str | None = None
    score: float | None = None
    hours_to_close: float | None = None
    order_id: str | None = None
    screened_price: float | None = None
    fill_price: float | None = None
    fill_time: datetime | None = None
    fill_slippage: float | None = None
    fill_slippage_pct: float | None = None
    outcome: TradeOutcome = TradeOutcome.PENDING
    resolution_price: float | None = None
    pnl: float | None = None
    execution_fee: float = 0.0
    paper_trade: bool = True
    strategy_name: str | None = None

    def __post_init__(self) -> None:
        _require_aware_datetime(self.timestamp, "timestamp")
        _require_text(self.market_id, "market_id")
        _require_text(self.market_question, "market_question")

        if not 0 < self.entry_price <= 1.0:
            raise ValueError("entry_price must be between 0 and 1")
        if self.position_size <= 0:
            raise ValueError("position_size must be positive")
        if self.score is not None and not 0 <= self.score <= 10:
            raise ValueError("score must be between 0 and 10 when provided")
        if self.hours_to_close is not None and self.hours_to_close < 0:
            raise ValueError("hours_to_close must be non-negative when provided")
        if self.screened_price is not None and not 0 < self.screened_price <= 1.0:
            raise ValueError("screened_price must be between 0 and 1 when provided")
        if self.fill_price is not None and not 0 < self.fill_price <= 1.0:
            raise ValueError("fill_price must be between 0 and 1 when provided")
        if self.fill_time is not None:
            _require_aware_datetime(self.fill_time, "fill_time")
        if self.fill_slippage is not None and not isinstance(self.fill_slippage, (int, float)):
            raise ValueError("fill_slippage must be numeric when provided")
        if self.fill_slippage_pct is not None and not isinstance(self.fill_slippage_pct, (int, float)):
            raise ValueError("fill_slippage_pct must be numeric when provided")
        if self.resolution_price is not None and not 0 <= self.resolution_price <= 1.0:
            raise ValueError("resolution_price must be between 0 and 1 when provided")
        if self.execution_fee < 0:
            raise ValueError("execution_fee must be non-negative")
        if self.strategy_name is not None and not self.strategy_name.strip():
            raise ValueError("strategy_name must not be empty when provided")
        if self.outcome is TradeOutcome.PENDING and self.pnl is not None:
            raise ValueError("Pending trades cannot have pnl assigned")
        if self.outcome in {TradeOutcome.WIN, TradeOutcome.LOSS} and self.resolution_price is None:
            raise ValueError("Resolved trades must define resolution_price")
        if self.screened_price is None:
            object.__setattr__(self, "screened_price", self.entry_price)
        if self.fill_price is not None and self.screened_price is not None:
            slippage = round(self.fill_price - self.screened_price, 6)
            slippage_pct = round(slippage / self.screened_price, 6) if self.screened_price > 0 else None
            if self.fill_slippage is None:
                object.__setattr__(self, "fill_slippage", slippage)
            if self.fill_slippage_pct is None:
                object.__setattr__(self, "fill_slippage_pct", slippage_pct)

    @property
    def contracts(self) -> float:
        price = self.fill_price or self.entry_price
        return self.position_size / price

    @property
    def is_resolved(self) -> bool:
        return self.outcome in {TradeOutcome.WIN, TradeOutcome.LOSS, TradeOutcome.CANCELLED}

    def settle(
        self,
        *,
        resolution_price: float,
        execution_fee: float = 0.0,
        settled_at: datetime | None = None,
    ) -> "Trade":
        if not 0 <= resolution_price <= 1.0:
            raise ValueError("resolution_price must be between 0 and 1")
        if execution_fee < 0:
            raise ValueError("execution_fee must be non-negative")

        fill_time = settled_at or datetime.now(UTC)
        _require_aware_datetime(fill_time, "settled_at")

        gross_payout = self.contracts * resolution_price
        pnl = round(gross_payout - self.position_size - execution_fee, 6)
        outcome = TradeOutcome.WIN if pnl > 0 else TradeOutcome.LOSS

        return replace(
            self,
            fill_time=self.fill_time or fill_time,
            outcome=outcome,
            resolution_price=resolution_price,
            pnl=pnl,
            execution_fee=execution_fee,
        )


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_aware_datetime(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware")
