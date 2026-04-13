from __future__ import annotations

from datetime import UTC, datetime

from models import OutcomeSide, Position, PositionStatus


def test_position_settle_closes_and_computes_pnl() -> None:
    position = Position(
        timestamp=datetime.now(UTC),
        market_id="m1",
        market_question="Question",
        side=OutcomeSide.YES,
        fill_price=0.53,
        shares=5.0,
        cost_basis=2.65,
        order_id="oid",
    )

    settled = position.settle(resolution_price=1.0)

    assert settled.status is PositionStatus.CLOSED
    assert settled.pnl == 2.35
