from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from models.enums import OutcomeSide, PositionStatus


@dataclass(frozen=True, slots=True)
class Position:
    timestamp: datetime
    market_id: str
    market_question: str
    side: OutcomeSide
    fill_price: float
    shares: float
    cost_basis: float
    position_id: int | None = None
    category: str | None = None
    order_id: str | None = None
    status: PositionStatus = PositionStatus.OPEN
    resolved_at: datetime | None = None
    resolution_price: float | None = None
    pnl: float | None = None
    paper_trade: bool = False
    strategy_name: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        _require_aware_datetime(self.timestamp, "timestamp")
        _require_text(self.market_id, "market_id")
        _require_text(self.market_question, "market_question")
        if self.position_id is not None and self.position_id <= 0:
            raise ValueError("position_id must be positive when provided")
        if not 0 < self.fill_price <= 1.0:
            raise ValueError("fill_price must be between 0 and 1")
        if self.shares <= 0:
            raise ValueError("shares must be positive")
        if self.cost_basis <= 0:
            raise ValueError("cost_basis must be positive")
        if self.resolved_at is not None:
            _require_aware_datetime(self.resolved_at, "resolved_at")
        if self.resolution_price is not None and not 0 <= self.resolution_price <= 1.0:
            raise ValueError("resolution_price must be between 0 and 1 when provided")
        if self.strategy_name is not None and not self.strategy_name.strip():
            raise ValueError("strategy_name must not be empty when provided")
        if self.session_id is not None and not self.session_id.strip():
            raise ValueError("session_id must not be empty when provided")
        if self.status is PositionStatus.OPEN and self.pnl is not None:
            raise ValueError("Open positions cannot have realized pnl")

    def settle(
        self,
        *,
        resolution_price: float,
        resolved_at: datetime | None = None,
        execution_fee: float = 0.0,
    ) -> "Position":
        if not 0 <= resolution_price <= 1.0:
            raise ValueError("resolution_price must be between 0 and 1")
        if execution_fee < 0:
            raise ValueError("execution_fee must be non-negative")
        resolved_time = resolved_at or datetime.now(UTC)
        _require_aware_datetime(resolved_time, "resolved_at")
        payout = self.shares * resolution_price
        pnl = round(payout - self.cost_basis - execution_fee, 6)
        return replace(
            self,
            status=PositionStatus.CLOSED,
            resolved_at=resolved_time,
            resolution_price=resolution_price,
            pnl=pnl,
        )


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_aware_datetime(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware")
