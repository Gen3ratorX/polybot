from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from models.enums import OrderAction, OrderStatus, OutcomeSide


@dataclass(frozen=True, slots=True)
class Order:
    created_at: datetime
    market_id: str
    token_id: str
    outcome_side: OutcomeSide
    action: OrderAction
    price: float
    size_usdc: float
    status: OrderStatus = OrderStatus.PENDING
    order_type: str = "GTC"
    order_id: str | None = None
    expires_at: datetime | None = None
    filled_size_usdc: float = 0.0

    def __post_init__(self) -> None:
        _require_aware_datetime(self.created_at, "created_at")
        _require_text(self.market_id, "market_id")
        _require_text(self.token_id, "token_id")
        _require_text(self.order_type, "order_type")

        if not 0 < self.price <= 1.0:
            raise ValueError("price must be between 0 and 1")
        if self.size_usdc <= 0:
            raise ValueError("size_usdc must be positive")
        if self.filled_size_usdc < 0:
            raise ValueError("filled_size_usdc must be non-negative")
        if self.filled_size_usdc > self.size_usdc:
            raise ValueError("filled_size_usdc cannot exceed size_usdc")
        if self.expires_at is not None:
            _require_aware_datetime(self.expires_at, "expires_at")
            if self.expires_at <= self.created_at:
                raise ValueError("expires_at must be after created_at")

    @property
    def remaining_size_usdc(self) -> float:
        return round(self.size_usdc - self.filled_size_usdc, 6)

    @property
    def is_open(self) -> bool:
        return self.status in {
            OrderStatus.PENDING,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        }


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_aware_datetime(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware")
