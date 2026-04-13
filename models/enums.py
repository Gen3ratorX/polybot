from __future__ import annotations

from enum import Enum


class OutcomeSide(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class TradeOutcome(str, Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    PENDING = "PENDING"
    CANCELLED = "CANCELLED"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
