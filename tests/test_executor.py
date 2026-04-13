from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bot.executor import OrderExecutor
from models import Market, OutcomeSide


@dataclass
class FakeLevel:
    price: str
    size: str


@dataclass
class FakeBook:
    bids: list[FakeLevel]
    asks: list[FakeLevel]
    tick_size: str
    min_order_size: str


class FakeClobClient:
    funder = "0xabc"

    def get_midpoint(self, token_id: str) -> dict[str, str]:
        return {"mid": "0.535"}

    def get_order_book(self, token_id: str) -> FakeBook:
        return FakeBook(
            bids=[FakeLevel(price="0.52", size="50"), FakeLevel(price="0.53", size="20")],
            asks=[FakeLevel(price="0.54", size="10"), FakeLevel(price="0.55", size="20")],
            tick_size="0.01",
            min_order_size="1",
        )

    def get_trades(self, params: Any):
        return []


class WideSpreadClobClient(FakeClobClient):
    def get_order_book(self, token_id: str) -> FakeBook:
        return FakeBook(
            bids=[FakeLevel(price="0.52", size="50"), FakeLevel(price="0.53", size="20")],
            asks=[FakeLevel(price="0.55", size="10"), FakeLevel(price="0.56", size="20")],
            tick_size="0.01",
            min_order_size="1",
        )


def test_executor_preview_uses_best_ask_when_price_omitted_for_taker_orders() -> None:
    executor = OrderExecutor(FakeClobClient())
    market = Market(
        market_id="540816",
        condition_id="0xcondition",
        question="Question",
        end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_price=0.53,
        no_price=0.47,
        volume=1000,
    )

    preview = executor.preview_limit_buy(
        market,
        side=OutcomeSide.YES,
        budget_usdc=1.0,
    )

    assert preview.token_id == "yes-token"
    assert preview.requested_budget_usdc == 1.0
    assert preview.limit_price == 0.55
    assert preview.shares == round(1.0 / 0.55, 6)
    assert preview.min_order_size == 1.0
    assert preview.minimum_budget_usdc == 0.55


def test_executor_preview_uses_one_tick_inside_spread_for_maker_orders() -> None:
    executor = OrderExecutor(WideSpreadClobClient(), execution_style="maker")
    market = Market(
        market_id="540816",
        condition_id="0xcondition",
        question="Question",
        end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_price=0.53,
        no_price=0.47,
        volume=1000,
    )

    preview = executor.preview_limit_buy(
        market,
        side=OutcomeSide.YES,
        budget_usdc=1.0,
    )

    assert preview.limit_price == 0.54


def test_executor_extract_order_id_from_common_shapes() -> None:
    assert OrderExecutor.extract_order_id({"orderID": "abc"}) == "abc"
    assert OrderExecutor.extract_order_id({"orderId": "abc"}) == "abc"
    assert OrderExecutor.extract_order_id({"id": "abc"}) == "abc"
    assert OrderExecutor.extract_order_id({"order": {"id": "abc"}}) == "abc"
    assert OrderExecutor.extract_order_id({"unexpected": "value"}) is None


def test_executor_preview_rejects_budget_below_minimum_order_size() -> None:
    class LargeMinBookClient(FakeClobClient):
        def get_order_book(self, token_id: str) -> FakeBook:
            return FakeBook(
                bids=[FakeLevel(price="0.52", size="50"), FakeLevel(price="0.53", size="20")],
                asks=[FakeLevel(price="0.54", size="10"), FakeLevel(price="0.55", size="20")],
                tick_size="0.01",
                min_order_size="5",
            )

    executor = OrderExecutor(LargeMinBookClient())
    market = Market(
        market_id="540816",
        condition_id="0xcondition",
        question="Question",
        end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_price=0.53,
        no_price=0.47,
        volume=1000,
    )

    try:
        executor.preview_limit_buy(market, side=OutcomeSide.YES, budget_usdc=1.0)
    except ValueError as exc:
        assert "Minimum size is 5 shares" in str(exc)
        assert "$2.75" in str(exc)
    else:
        raise AssertionError("Expected preview to reject undersized order budget")


def test_executor_preview_uses_minimum_valid_budget_when_enabled() -> None:
    class LargeMinBookClient(FakeClobClient):
        def get_order_book(self, token_id: str) -> FakeBook:
            return FakeBook(
                bids=[FakeLevel(price="0.52", size="50"), FakeLevel(price="0.53", size="20")],
                asks=[FakeLevel(price="0.54", size="10"), FakeLevel(price="0.55", size="20")],
                tick_size="0.01",
                min_order_size="5",
            )

    executor = OrderExecutor(LargeMinBookClient())
    market = Market(
        market_id="540816",
        condition_id="0xcondition",
        question="Question",
        end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_price=0.53,
        no_price=0.47,
        volume=1000,
    )

    preview = executor.preview_limit_buy(
        market,
        side=OutcomeSide.YES,
        budget_usdc=5.0,
        use_minimum_budget=True,
    )

    assert preview.budget_usdc == 2.75
    assert preview.requested_budget_usdc == 5.0
    assert preview.shares == 5.0


def test_executor_preview_limit_sell_uses_best_bid() -> None:
    executor = OrderExecutor(FakeClobClient())
    market = Market(
        market_id="540816",
        condition_id="0xcondition",
        question="Question",
        end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_price=0.53,
        no_price=0.47,
        volume=1000,
    )

    preview = executor.preview_limit_sell(
        market,
        side=OutcomeSide.YES,
        shares=5.2,
    )

    assert preview.token_id == "yes-token"
    assert preview.limit_price == 0.53
    assert preview.estimated_proceeds_usdc == round(5.2 * 0.53, 6)


def test_parse_order_status_accepts_websocket_style_field_names() -> None:
    status = OrderExecutor.parse_order_status(
        {
            "id": "oid",
            "status": "matched",
            "createdAtMs": 1712400000000,
            "market_id": "cond",
            "assetId": "asset-1",
            "side": "BUY",
            "price": "0.53",
            "originalSize": "5.66",
            "sizeMatched": "5.66",
        }
    )

    assert status.order_id == "oid"
    assert status.status == "FILLED"
    assert status.market_condition_id == "cond"
    assert status.token_id == "asset-1"
    assert status.matched_size == 5.66


def test_parse_order_status_normalizes_resting_and_partial_fills() -> None:
    resting = OrderExecutor.parse_order_status(
        {
            "id": "oid-resting",
            "status": "LIVE",
            "createdAtMs": 1712400000000,
            "assetId": "asset-1",
            "originalSize": "5.0",
            "sizeMatched": "0.0",
            "price": "0.53",
        }
    )
    partial = OrderExecutor.parse_order_status(
        {
            "id": "oid-partial",
            "status": "LIVE",
            "createdAtMs": 1712400000000,
            "assetId": "asset-1",
            "originalSize": "5.0",
            "sizeMatched": "2.5",
            "price": "0.53",
        }
    )

    assert resting.status == "LIVE_RESTING"
    assert resting.is_terminal is False
    assert partial.status == "PARTIALLY_FILLED"
    assert partial.has_fill is True


def test_reconcile_fill_status_uses_recent_maker_fill_when_final_status_looks_cancelled() -> None:
    class FilledTradeClient(FakeClobClient):
        def get_trades(self, params: Any):
            return [
                {
                    "trader_side": "MAKER",
                    "match_time": str(int(datetime(2026, 4, 7, 16, 21, 14, tzinfo=UTC).timestamp())),
                    "maker_orders": [
                        {
                            "maker_address": "0xabc",
                            "asset_id": "yes-token",
                            "side": "BUY",
                            "matched_amount": "5.2",
                            "price": "0.53",
                        }
                    ],
                }
            ]

    executor = OrderExecutor(FilledTradeClient())
    status = executor.reconcile_fill_status(
        _preview(),
        executor.parse_order_status(
            {
                "id": "oid",
                "status": "CANCELED",
                "createdAtMs": 1712400000000,
                "assetId": "yes-token",
                "originalSize": "5.66",
                "sizeMatched": "0.0",
                "price": "0.53",
            }
        ),
        submitted_at=datetime(2026, 4, 7, 16, 21, 13, tzinfo=UTC),
    )

    assert status.status == "CANCELLED"
    assert status.matched_size == 5.2
    assert status.has_fill is True


def _preview():
    from bot.executor import LiveOrderPreview

    return LiveOrderPreview(
        market_id="540816",
        question="Question",
        outcome_side=OutcomeSide.YES.value,
        token_id="yes-token",
        requested_budget_usdc=3.0,
        budget_usdc=3.0,
        limit_price=0.53,
        shares=5.660377,
        midpoint=0.535,
        best_bid=0.53,
        best_ask=0.54,
        tick_size=0.01,
        min_order_size=5.0,
        minimum_budget_usdc=2.65,
    )
