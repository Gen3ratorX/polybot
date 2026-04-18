from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.reconciler import reconcile_manual_exits, reconcile_open_orders, reconcile_open_positions
from bot.tracker import TradeTracker
from models import Market, OutcomeSide, Trade, TradeOutcome


class FakeGammaClient:
    async def fetch_market(self, market_id: str) -> Market:
        return Market(
            market_id=market_id,
            condition_id="cond",
            question="Question",
            end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
            yes_token_id="yes-token",
            no_token_id="no-token",
            yes_price=1.0,
            no_price=0.0,
            volume=1000,
            active=False,
            closed=True,
        )


class FakeClobClient:
    funder = "0xabc"

    def __init__(
        self,
        *,
        order_payloads: dict[str, dict[str, object]] | None = None,
        trades_by_token: dict[str, list[dict[str, object]]] | None = None,
    ) -> None:
        self._order_payloads = order_payloads or {}
        self._trades_by_token = trades_by_token or {}

    def get_order(self, order_id: str) -> dict[str, object]:
        return self._order_payloads[order_id]

    def get_trades(self, params=None):
        token_id = getattr(params, "asset_id", None)
        if token_id is None:
            trades: list[dict[str, object]] = []
            for payloads in self._trades_by_token.values():
                trades.extend(payloads)
            return trades
        return list(self._trades_by_token.get(str(token_id), []))


def _maker_trade_payload(
    *,
    token_id: str,
    side: str,
    matched_amount: float,
    price: float,
    match_time: datetime,
) -> dict[str, object]:
    return {
        "trader_side": "MAKER",
        "match_time": str(int(match_time.timestamp())),
        "maker_orders": [
            {
                "maker_address": "0xabc",
                "asset_id": token_id,
                "side": side,
                "matched_amount": str(matched_amount),
                "price": str(price),
            }
        ],
    }


def _order_payload(
    *,
    order_id: str,
    status: str,
    side: str,
    token_id: str = "yes-token",
    price: float = 0.96,
    original_size: float = 5.0,
    size_matched: float = 0.0,
    created_at: datetime | None = None,
) -> dict[str, object]:
    created_at = created_at or datetime(2026, 4, 7, 16, 0, tzinfo=UTC)
    return {
        "id": order_id,
        "status": status,
        "createdAtMs": int(created_at.timestamp() * 1000),
        "assetId": token_id,
        "side": side,
        "price": str(price),
        "originalSize": str(original_size),
        "sizeMatched": str(size_matched),
        "market_id": "m1",
    }


@pytest.mark.asyncio
async def test_reconcile_open_orders_updates_live_resting_partial_fill(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'orders-partial.db'}")
    tracker.initialize()

    order_id = "oid-partial"
    submitted_at = datetime(2026, 4, 7, 16, 0, tzinfo=UTC)
    tracker.record_order(
        order_id=order_id,
        market_id="m1",
        status="LIVE_RESTING",
        requested_size=5.0,
        limit_price=0.95,
        filled_size=0.0,
        last_seen_status="LIVE_RESTING",
        last_seen_at=submitted_at,
    )

    client = FakeClobClient(
        order_payloads={
            order_id: _order_payload(
                order_id=order_id,
                status="LIVE",
                side="BUY",
                size_matched=0.0,
                created_at=submitted_at,
            )
        },
        trades_by_token={
            "yes-token": [
                _maker_trade_payload(
                    token_id="yes-token",
                    side="BUY",
                    matched_amount=2.5,
                    price=0.96,
                    match_time=submitted_at + timedelta(seconds=30),
                )
            ]
        },
    )

    results = await reconcile_open_orders(tracker, FakeGammaClient(), client)

    assert len(results) == 1
    assert results[0].previous_status == "LIVE_RESTING"
    assert results[0].current_status == "PARTIALLY_FILLED"
    assert results[0].provisional_cancel_corrected is False
    assert results[0].created_position is True

    stored = tracker.get_order_by_order_id(order_id)
    assert stored is not None
    assert stored["status"] == "PARTIALLY_FILLED"
    assert stored["filled_size"] == 2.5
    assert stored["limit_price"] == 0.95
    assert tracker.trade_count() == 1
    assert tracker.open_order_count() == 1
    assert tracker.open_position_count() == 1
    assert tracker.list_recent_trades(limit=1)[0].screened_price == 0.95
    assert tracker.list_recent_trades(limit=1)[0].fill_slippage == pytest.approx(0.01, rel=1e-6)


@pytest.mark.asyncio
async def test_reconcile_open_orders_updates_live_resting_complete_fill(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'orders-filled.db'}")
    tracker.initialize()

    order_id = "oid-filled"
    submitted_at = datetime(2026, 4, 7, 16, 0, tzinfo=UTC)
    tracker.record_order(
        order_id=order_id,
        market_id="m1",
        status="LIVE_RESTING",
        requested_size=5.0,
        limit_price=0.95,
        filled_size=0.0,
        last_seen_status="LIVE_RESTING",
        last_seen_at=submitted_at,
    )

    client = FakeClobClient(
        order_payloads={
            order_id: _order_payload(
                order_id=order_id,
                status="LIVE",
                side="BUY",
                size_matched=0.0,
                created_at=submitted_at,
            )
        },
        trades_by_token={
            "yes-token": [
                _maker_trade_payload(
                    token_id="yes-token",
                    side="BUY",
                    matched_amount=5.0,
                    price=0.96,
                    match_time=submitted_at + timedelta(seconds=30),
                )
            ]
        },
    )

    results = await reconcile_open_orders(tracker, FakeGammaClient(), client)

    assert len(results) == 1
    assert results[0].previous_status == "LIVE_RESTING"
    assert results[0].current_status == "FILLED"
    assert results[0].created_position is True

    stored = tracker.get_order_by_order_id(order_id)
    assert stored is not None
    assert stored["status"] == "FILLED"
    assert stored["filled_size"] == 5.0
    assert stored["limit_price"] == 0.95
    assert tracker.trade_count() == 1
    assert tracker.open_order_count() == 0
    assert tracker.open_position_count() == 1


@pytest.mark.asyncio
async def test_reconcile_open_orders_grows_existing_position_on_later_partial_fill(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'orders-growing.db'}")
    tracker.initialize()

    order_id = "oid-growing"
    submitted_at = datetime(2026, 4, 7, 16, 0, tzinfo=UTC)
    tracker.record_order(
        order_id=order_id,
        market_id="m1",
        status="PARTIALLY_FILLED",
        requested_size=5.0,
        limit_price=0.95,
        filled_size=2.5,
        last_seen_status="PARTIALLY_FILLED",
        last_seen_at=submitted_at,
    )
    initial_trade = Trade(
        timestamp=submitted_at,
        market_id="m1",
        market_question="Question",
        category="sports",
        side=OutcomeSide.YES,
        entry_price=0.95,
        position_size=2.375,
        order_id=order_id,
        fill_price=0.95,
        fill_time=submitted_at,
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(initial_trade)
    tracker.register_open_position_from_trade(initial_trade)

    client = FakeClobClient(
        order_payloads={
            order_id: _order_payload(
                order_id=order_id,
                status="LIVE",
                side="BUY",
                size_matched=0.0,
                created_at=submitted_at,
            )
        },
        trades_by_token={
            "yes-token": [
                _maker_trade_payload(
                    token_id="yes-token",
                    side="BUY",
                    matched_amount=5.0,
                    price=0.95,
                    match_time=submitted_at + timedelta(seconds=30),
                )
            ]
        },
    )

    results = await reconcile_open_orders(tracker, FakeGammaClient(), client)

    assert len(results) == 1
    assert results[0].previous_status == "PARTIALLY_FILLED"
    assert results[0].current_status == "FILLED"

    stored = tracker.get_order_by_order_id(order_id)
    assert stored is not None
    assert stored["filled_size"] == 5.0

    position = tracker.get_position_by_order_id(order_id)
    assert position is not None
    assert position.shares == 5.0
    assert position.cost_basis == pytest.approx(4.75, rel=1e-6)
    assert tracker.open_position_count() == 1


@pytest.mark.asyncio
async def test_reconcile_open_orders_corrects_provisional_cancel_to_partial_fill(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'orders-cancel.db'}")
    tracker.initialize()

    order_id = "oid-cancelled"
    submitted_at = datetime(2026, 4, 7, 16, 0, tzinfo=UTC)
    tracker.record_order(
        order_id=order_id,
        market_id="m1",
        status="CANCELLED",
        requested_size=5.0,
        limit_price=0.95,
        filled_size=0.0,
        last_seen_status="CANCELLED",
        last_seen_at=submitted_at,
    )

    client = FakeClobClient(
        order_payloads={
            order_id: _order_payload(
                order_id=order_id,
                status="CANCELED",
                side="BUY",
                size_matched=0.0,
                created_at=submitted_at,
            )
        },
        trades_by_token={
            "yes-token": [
                _maker_trade_payload(
                    token_id="yes-token",
                    side="BUY",
                    matched_amount=2.5,
                    price=0.96,
                    match_time=submitted_at + timedelta(seconds=45),
                )
            ]
        },
    )

    results = await reconcile_open_orders(tracker, FakeGammaClient(), client)

    assert len(results) == 1
    assert results[0].previous_status == "CANCELLED"
    assert results[0].current_status == "PARTIALLY_FILLED"
    assert results[0].provisional_cancel_corrected is True
    assert results[0].created_position is True

    stored = tracker.get_order_by_order_id(order_id)
    assert stored is not None
    assert stored["status"] == "PARTIALLY_FILLED"
    assert stored["filled_size"] == 2.5
    assert stored["limit_price"] == 0.95
    assert tracker.trade_count() == 1
    assert tracker.open_order_count() == 1
    assert tracker.open_position_count() == 1


@pytest.mark.asyncio
async def test_reconcile_open_orders_handles_open_status_after_crash(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'orders-open.db'}")
    tracker.initialize()

    order_id = "oid-open"
    submitted_at = datetime(2026, 4, 7, 16, 0, tzinfo=UTC)
    tracker.record_order(
        order_id=order_id,
        market_id="m1",
        status="OPEN",
        requested_size=5.0,
        limit_price=0.95,
        filled_size=0.0,
        last_seen_status="OPEN",
        last_seen_at=submitted_at,
    )

    client = FakeClobClient(
        order_payloads={
            order_id: _order_payload(
                order_id=order_id,
                status="LIVE",
                side="BUY",
                size_matched=0.0,
                created_at=submitted_at,
            )
        }
    )

    results = await reconcile_open_orders(tracker, FakeGammaClient(), client)

    assert len(results) == 1
    assert results[0].previous_status == "OPEN"
    assert results[0].current_status == "LIVE_RESTING"
    assert results[0].provisional_cancel_corrected is False
    assert results[0].created_position is False

    stored = tracker.get_order_by_order_id(order_id)
    assert stored is not None
    assert stored["status"] == "LIVE_RESTING"
    assert stored["filled_size"] == 0.0
    assert tracker.open_order_count() == 1
    assert tracker.open_position_count() == 0


@pytest.mark.asyncio
async def test_reconcile_open_positions_closes_position_and_updates_trade(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'reconcile.db'}")
    tracker.initialize()

    trade = Trade(
        timestamp=datetime.now(UTC) - timedelta(minutes=10),
        market_id="m1",
        market_question="Question",
        category="sports",
        side=OutcomeSide.YES,
        entry_price=0.53,
        position_size=2.65,
        order_id="oid",
        fill_price=0.53,
        fill_time=datetime.now(UTC),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(trade)
    tracker.register_open_position_from_trade(trade)

    results = await reconcile_open_positions(tracker, FakeGammaClient())

    assert len(results) == 1
    assert results[0].outcome is TradeOutcome.WIN
    assert tracker.open_position_count() == 0
    updated_trade = tracker.list_recent_trades(limit=1)[0]
    assert updated_trade.outcome is TradeOutcome.WIN
    assert updated_trade.pnl == 2.35


@pytest.mark.asyncio
async def test_reconcile_manual_exits_closes_position_and_updates_trade(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'manual-exit.db'}")
    tracker.initialize()

    trade = Trade(
        timestamp=datetime(2026, 4, 7, 16, 0, tzinfo=UTC),
        market_id="m1",
        market_question="Question",
        category="sports",
        side=OutcomeSide.YES,
        entry_price=0.90,
        position_size=4.5,
        order_id="oid",
        fill_price=0.90,
        fill_time=datetime(2026, 4, 7, 16, 0, 5, tzinfo=UTC),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(trade)
    tracker.register_open_position_from_trade(trade)

    results = await reconcile_manual_exits(
        tracker,
        FakeGammaClient(),
        clob_client=FakeClobClient(
            trades_by_token={
                "yes-token": [
                    _maker_trade_payload(
                        token_id="yes-token",
                        side="SELL",
                        matched_amount=5.0,
                        price=0.99,
                        match_time=datetime(2026, 4, 7, 16, 30, tzinfo=UTC),
                    )
                ]
            }
        ),
        wallet_address="0xabc",
    )

    assert len(results) == 1
    assert results[0].outcome is TradeOutcome.WIN
    assert tracker.open_position_count() == 0
    updated_trade = tracker.list_recent_trades(limit=1)[0]
    assert updated_trade.outcome is TradeOutcome.WIN
    assert updated_trade.resolution_price == 0.99
