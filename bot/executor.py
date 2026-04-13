from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from math import floor
from typing import Any

from py_clob_client.clob_types import OrderArgs, OrderBookSummary, TradeParams

from models import Market, OutcomeSide, Trade, TradeOutcome


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    token_id: str
    midpoint: float | None
    best_bid: float | None
    best_ask: float | None
    tick_size: float
    min_order_size: float | None


@dataclass(frozen=True, slots=True)
class LiveOrderPreview:
    market_id: str
    question: str
    outcome_side: str
    token_id: str
    requested_budget_usdc: float
    budget_usdc: float
    limit_price: float
    shares: float
    midpoint: float | None
    best_bid: float | None
    best_ask: float | None
    tick_size: float
    min_order_size: float | None
    minimum_budget_usdc: float | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LiveExitPreview:
    market_id: str
    question: str
    outcome_side: str
    token_id: str
    shares: float
    limit_price: float
    midpoint: float | None
    best_bid: float | None
    best_ask: float | None
    tick_size: float
    estimated_proceeds_usdc: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LiveOrderStatus:
    order_id: str
    status: str
    created_at: datetime | None
    market_condition_id: str | None
    token_id: str | None
    side: str | None
    price: float | None
    original_size: float
    matched_size: float

    @property
    def is_terminal(self) -> bool:
        return self.status in {"CANCELED", "CANCELLED", "FILLED", "MATCHED", "REJECTED"}

    @property
    def has_fill(self) -> bool:
        return self.matched_size > 0


@dataclass(frozen=True, slots=True)
class TradeFillMatch:
    matched_size: float
    fill_price: float
    fill_time: datetime | None


class OrderExecutor:
    def __init__(
        self,
        clob_client: Any,
        *,
        execution_style: str = "taker",
        post_only_emulation: bool = True,
    ) -> None:
        self.clob_client = clob_client
        self.execution_style = _normalize_execution_style(execution_style)
        self.post_only_emulation = post_only_emulation

    def get_order_book_snapshot(self, token_id: str) -> OrderBookSnapshot:
        midpoint_response = self.clob_client.get_midpoint(token_id)
        midpoint = _extract_float(midpoint_response, "mid")

        book: OrderBookSummary = self.clob_client.get_order_book(token_id)
        best_bid = float(book.bids[-1].price) if book.bids else None
        best_ask = float(book.asks[-1].price) if book.asks else None

        return OrderBookSnapshot(
            token_id=token_id,
            midpoint=midpoint,
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=float(book.tick_size),
            min_order_size=float(book.min_order_size) if book.min_order_size is not None else None,
        )

    def preview_limit_buy(
        self,
        market: Market,
        *,
        side: OutcomeSide,
        budget_usdc: float,
        limit_price: float | None = None,
        use_minimum_budget: bool = False,
        execution_style: str | None = None,
    ) -> LiveOrderPreview:
        if budget_usdc <= 0:
            raise ValueError("budget_usdc must be positive")

        token_id = market.token_id_for(side)
        snapshot = self.get_order_book_snapshot(token_id)
        style = _normalize_execution_style(execution_style or self.execution_style)
        price = limit_price if limit_price is not None else self._select_buy_limit_price(
            snapshot,
            execution_style=style,
        )
        if price is None:
            raise ValueError("Could not determine a usable limit price for this token")

        rounded_price = _round_to_tick(price, snapshot.tick_size)
        if rounded_price <= 0:
            raise ValueError("Rounded limit price must be positive")

        effective_budget = budget_usdc
        shares = round(effective_budget / rounded_price, 6)
        if shares <= 0:
            raise ValueError("Derived share size must be positive")

        minimum_budget_usdc = None
        if snapshot.min_order_size is not None:
            minimum_budget_usdc = round(snapshot.min_order_size * rounded_price, 6)
            if use_minimum_budget:
                effective_budget = minimum_budget_usdc
                shares = round(effective_budget / rounded_price, 6)
            if shares < snapshot.min_order_size:
                raise ValueError(
                    "Budget is too small for this market at the selected price. "
                    f"Minimum size is {snapshot.min_order_size:g} shares, "
                    f"which requires at least ${minimum_budget_usdc:.2f}."
                )

        return LiveOrderPreview(
            market_id=market.market_id,
            question=market.question,
            outcome_side=side.value,
            token_id=token_id,
            requested_budget_usdc=round(budget_usdc, 6),
            budget_usdc=round(effective_budget, 6),
            limit_price=rounded_price,
            shares=shares,
            midpoint=snapshot.midpoint,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            tick_size=snapshot.tick_size,
            min_order_size=snapshot.min_order_size,
            minimum_budget_usdc=minimum_budget_usdc,
        )

    def place_limit_buy(self, preview: LiveOrderPreview) -> dict[str, object]:
        order = self.clob_client.create_order(
            OrderArgs(
                token_id=preview.token_id,
                price=preview.limit_price,
                size=preview.shares,
                side="BUY",
            )
        )
        response = self.clob_client.post_order(order)
        if not isinstance(response, dict):
            raise ValueError("Expected post_order response to be a dictionary")
        if self.execution_style == "maker" and self.post_only_emulation:
            response = self._maybe_emulate_post_only_buy(preview, response)
        return response

    def preview_limit_sell(
        self,
        market: Market,
        *,
        side: OutcomeSide,
        shares: float,
        limit_price: float | None = None,
        execution_style: str | None = None,
    ) -> LiveExitPreview:
        if shares <= 0:
            raise ValueError("shares must be positive")
        token_id = market.token_id_for(side)
        snapshot = self.get_order_book_snapshot(token_id)
        style = _normalize_execution_style(execution_style or self.execution_style)
        price = limit_price if limit_price is not None else self._select_sell_limit_price(
            snapshot,
            execution_style=style,
        )
        if price is None:
            raise ValueError("Could not determine a usable limit exit price for this token")
        rounded_price = _round_to_tick(price, snapshot.tick_size)
        if rounded_price <= 0:
            raise ValueError("Rounded limit price must be positive")
        return LiveExitPreview(
            market_id=market.market_id,
            question=market.question,
            outcome_side=side.value,
            token_id=token_id,
            shares=round(shares, 6),
            limit_price=rounded_price,
            midpoint=snapshot.midpoint,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            tick_size=snapshot.tick_size,
            estimated_proceeds_usdc=round(shares * rounded_price, 6),
        )

    def place_limit_sell(self, preview: LiveExitPreview) -> dict[str, object]:
        order = self.clob_client.create_order(
            OrderArgs(
                token_id=preview.token_id,
                price=preview.limit_price,
                size=preview.shares,
                side="SELL",
            )
        )
        response = self.clob_client.post_order(order)
        if not isinstance(response, dict):
            raise ValueError("Expected post_order response to be a dictionary")
        if self.execution_style == "maker" and self.post_only_emulation:
            response = self._maybe_emulate_post_only_sell(preview, response)
        return response

    def cancel_order(self, order_id: str) -> dict[str, object]:
        response = self.clob_client.cancel(order_id)
        if not isinstance(response, dict):
            raise ValueError("Expected cancel response to be a dictionary")
        return response

    def get_order(self, order_id: str) -> dict[str, object]:
        response = self.clob_client.get_order(order_id)
        if not isinstance(response, dict):
            raise ValueError("Expected get_order response to be a dictionary")
        return response

    def get_order_status(self, order_id: str) -> LiveOrderStatus:
        payload = self.get_order(order_id)
        return self.parse_order_status(payload)

    def reconcile_fill_status(
        self,
        preview: LiveOrderPreview,
        order_status: LiveOrderStatus,
        *,
        submitted_at: datetime,
        side: str = "BUY",
    ) -> LiveOrderStatus:
        if order_status.has_fill:
            return order_status
        fill_match = self.find_recent_fill_match(
            preview,
            submitted_at=submitted_at,
            side=side,
        )
        if fill_match is None:
            return order_status
        return LiveOrderStatus(
            order_id=order_status.order_id,
            status=order_status.status,
            created_at=fill_match.fill_time or order_status.created_at,
            market_condition_id=order_status.market_condition_id,
            token_id=order_status.token_id or preview.token_id,
            side=order_status.side or side,
            price=fill_match.fill_price,
            original_size=order_status.original_size or preview.shares,
            matched_size=fill_match.matched_size,
        )

    def find_recent_fill_match(
        self,
        preview: LiveOrderPreview,
        *,
        submitted_at: datetime,
        side: str = "BUY",
        lookback_seconds: int = 300,
        time_tolerance_seconds: int = 5,
    ) -> TradeFillMatch | None:
        trades = self.clob_client.get_trades(
            TradeParams(
                asset_id=preview.token_id,
                after=max(0, int(submitted_at.timestamp()) - lookback_seconds),
            )
        )
        if not isinstance(trades, list):
            return None

        wallet_address = _normalized_wallet_address(getattr(self.clob_client, "funder", None))
        matched_size = 0.0
        weighted_notional = 0.0
        fill_time: datetime | None = None

        for payload in trades:
            candidate = _extract_fill_candidate(
                payload,
                token_id=preview.token_id,
                side=side,
                wallet_address=wallet_address,
                preview_price=preview.limit_price,
                submitted_at=submitted_at,
                time_tolerance_seconds=time_tolerance_seconds,
            )
            if candidate is None:
                continue
            matched_size += candidate.matched_size
            weighted_notional += candidate.matched_size * candidate.fill_price
            if fill_time is None or (
                candidate.fill_time is not None and candidate.fill_time < fill_time
            ):
                fill_time = candidate.fill_time

        if matched_size <= 0:
            return None

        return TradeFillMatch(
            matched_size=round(matched_size, 6),
            fill_price=round(weighted_notional / matched_size, 6),
            fill_time=fill_time,
        )

    @staticmethod
    def parse_order_status(payload: dict[str, object]) -> LiveOrderStatus:
        order_id = OrderExecutor.extract_order_id(payload)
        if order_id is None:
            raise ValueError("Could not extract order id from order payload")

        created_at = _parse_created_at(
            payload.get("created_at")
            or payload.get("createdAt")
            or payload.get("created_at_ms")
            or payload.get("createdAtMs")
            or payload.get("timestamp")
        )
        raw_status = str(payload.get("status", "UNKNOWN")).strip().upper()
        original_size = _to_optional_float(
            payload.get("original_size") or payload.get("originalSize") or payload.get("size")
        ) or 0.0
        matched_size = _to_optional_float(
            payload.get("size_matched")
            or payload.get("sizeMatched")
            or payload.get("matched_size")
        ) or 0.0

        return LiveOrderStatus(
            order_id=order_id,
            status=_normalize_live_order_status(
                raw_status,
                matched_size=matched_size,
                original_size=original_size,
            ),
            created_at=created_at,
            market_condition_id=_as_optional_str(
                payload.get("market") or payload.get("market_id") or payload.get("condition_id")
            ),
            token_id=_as_optional_str(payload.get("asset_id") or payload.get("assetId")),
            side=_as_optional_str(payload.get("side")),
            price=_to_optional_float(payload.get("price")),
            original_size=original_size,
            matched_size=matched_size,
        )

    @staticmethod
    def build_trade_record(
        market: Market,
        preview: LiveOrderPreview,
        order_status: LiveOrderStatus,
        *,
        recorded_at: datetime | None = None,
        strategy_name: str | None = None,
    ) -> Trade:
        timestamp = recorded_at or datetime.now(UTC)
        fill_price = order_status.price or preview.limit_price
        matched_budget = round(order_status.matched_size * fill_price, 6)
        hours_to_close = max(0.0, market.hours_to_close(timestamp))

        if order_status.has_fill:
            return Trade(
                timestamp=timestamp,
                market_id=market.market_id,
                market_question=market.question,
                category=market.category,
                side=OutcomeSide(preview.outcome_side),
                entry_price=preview.limit_price,
                position_size=matched_budget,
                score=None,
                hours_to_close=hours_to_close,
                order_id=order_status.order_id,
                screened_price=preview.limit_price,
                fill_price=fill_price,
                fill_time=order_status.created_at or timestamp,
                outcome=TradeOutcome.PENDING,
                paper_trade=False,
                strategy_name=strategy_name,
            )

        return Trade(
            timestamp=timestamp,
            market_id=market.market_id,
            market_question=market.question,
            category=market.category,
            side=OutcomeSide(preview.outcome_side),
            entry_price=preview.limit_price,
            position_size=preview.budget_usdc,
            score=None,
            hours_to_close=hours_to_close,
            order_id=order_status.order_id,
            screened_price=preview.limit_price,
            outcome=TradeOutcome.CANCELLED,
            paper_trade=False,
            strategy_name=strategy_name,
        )

    @staticmethod
    def extract_order_id(payload: dict[str, object]) -> str | None:
        for key in ("orderID", "orderId", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        nested = payload.get("order")
        if isinstance(nested, dict):
            for key in ("id", "orderID", "orderId"):
                value = nested.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    def _select_buy_limit_price(
        self,
        snapshot: OrderBookSnapshot,
        *,
        execution_style: str,
    ) -> float | None:
        if execution_style == "maker":
            if snapshot.best_bid is not None and snapshot.best_ask is not None:
                one_tick_inside = _round_to_tick(snapshot.best_bid + snapshot.tick_size, snapshot.tick_size)
                if one_tick_inside < snapshot.best_ask - 1e-9:
                    return one_tick_inside
            if snapshot.best_bid is not None:
                return snapshot.best_bid
            if snapshot.best_ask is not None:
                return _round_to_tick(max(snapshot.best_ask - snapshot.tick_size, snapshot.tick_size), snapshot.tick_size)
            return snapshot.midpoint
        if snapshot.best_ask is not None:
            return snapshot.best_ask
        if snapshot.best_bid is not None:
            return _round_to_tick(min(snapshot.best_bid + snapshot.tick_size, 1.0), snapshot.tick_size)
        return snapshot.midpoint

    def _select_sell_limit_price(
        self,
        snapshot: OrderBookSnapshot,
        *,
        execution_style: str,
    ) -> float | None:
        if execution_style == "maker":
            if snapshot.best_bid is not None and snapshot.best_ask is not None:
                one_tick_inside = _round_to_tick(snapshot.best_ask - snapshot.tick_size, snapshot.tick_size)
                if one_tick_inside > snapshot.best_bid + 1e-9:
                    return one_tick_inside
            if snapshot.best_ask is not None:
                return snapshot.best_ask
            if snapshot.best_bid is not None:
                return _round_to_tick(min(snapshot.best_bid + snapshot.tick_size, 1.0), snapshot.tick_size)
            return snapshot.midpoint
        if snapshot.best_bid is not None:
            return snapshot.best_bid
        if snapshot.best_ask is not None:
            return _round_to_tick(max(snapshot.best_ask - snapshot.tick_size, snapshot.tick_size), snapshot.tick_size)
        return snapshot.midpoint

    def _maybe_emulate_post_only_buy(
        self,
        preview: LiveOrderPreview,
        response: dict[str, object],
    ) -> dict[str, object]:
        order_id = self.extract_order_id(response)
        if not order_id:
            return response
        try:
            status = self.get_order_status(order_id)
        except Exception:
            return response
        if not status.has_fill:
            return response

        emulation = {
            "observed_status": status.status,
            "matched_size": status.matched_size,
            "original_size": status.original_size,
            "reprice_required": status.status != "FILLED",
        }
        if status.status != "FILLED" and status.matched_size < status.original_size:
            try:
                emulation["cancel_response"] = self.cancel_order(order_id)
            except Exception as exc:
                emulation["cancel_error"] = str(exc)
        if emulation["reprice_required"]:
            emulation["recommended_action"] = "REPRICE"
        enriched = dict(response)
        enriched["post_only_emulation"] = emulation
        enriched["post_only_preview"] = preview.as_dict()
        return enriched

    def _maybe_emulate_post_only_sell(
        self,
        preview: LiveExitPreview,
        response: dict[str, object],
    ) -> dict[str, object]:
        order_id = self.extract_order_id(response)
        if not order_id:
            return response
        try:
            status = self.get_order_status(order_id)
        except Exception:
            return response
        if not status.has_fill:
            return response

        emulation = {
            "observed_status": status.status,
            "matched_size": status.matched_size,
            "original_size": status.original_size,
            "reprice_required": status.status != "FILLED",
        }
        if status.status != "FILLED" and status.matched_size < status.original_size:
            try:
                emulation["cancel_response"] = self.cancel_order(order_id)
            except Exception as exc:
                emulation["cancel_error"] = str(exc)
        if emulation["reprice_required"]:
            emulation["recommended_action"] = "REPRICE"
        enriched = dict(response)
        enriched["post_only_emulation"] = emulation
        enriched["post_only_preview"] = preview.as_dict()
        return enriched


def _extract_float(payload: dict[str, object], key: str) -> float | None:
    value = payload.get(key)
    if value is None:
        return None
    return float(value)


def _round_to_tick(price: float, tick_size: float) -> float:
    steps = floor((price / tick_size) + 1e-9)
    return round(steps * tick_size, 8)


def _normalize_execution_style(value: str | None) -> str:
    normalized = (value or "taker").strip().lower()
    if normalized not in {"maker", "taker"}:
        raise ValueError(f"Unsupported execution_style: {value!r}")
    return normalized


def _normalize_live_order_status(
    raw_status: str,
    *,
    matched_size: float,
    original_size: float,
) -> str:
    if raw_status in {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
        return _normalize_execution_status_alias(raw_status)
    if matched_size > 0:
        if original_size > 0 and matched_size + 1e-9 >= original_size:
            return "FILLED"
        return "PARTIALLY_FILLED"
    if raw_status in {"MATCHED", "FILLED"}:
        return "FILLED"
    if raw_status in {"LIVE", "OPEN", "RESTING"}:
        return "LIVE_RESTING"
    return _normalize_execution_status_alias(raw_status)


def _normalize_execution_status_alias(value: str) -> str:
    aliases = {
        "CANCELED": "CANCELLED",
        "MATCHED": "FILLED",
        "LIVE": "LIVE_RESTING",
        "OPEN": "LIVE_RESTING",
        "RESTING": "LIVE_RESTING",
    }
    return aliases.get(value, value)


def _to_optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _as_optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _parse_created_at(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1_000_000_000_000:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        if value.isdigit():
            seconds = float(value)
            if seconds > 1_000_000_000_000:
                seconds /= 1000
            return datetime.fromtimestamp(seconds, tz=UTC)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _extract_fill_candidate(
    payload: object,
    *,
    token_id: str,
    side: str,
    wallet_address: str | None,
    preview_price: float,
    submitted_at: datetime,
    time_tolerance_seconds: int,
) -> TradeFillMatch | None:
    if not isinstance(payload, dict):
        return None

    match_time = _parse_created_at(payload.get("match_time") or payload.get("last_update"))
    if match_time is not None and match_time < submitted_at - timedelta(seconds=time_tolerance_seconds):
        return None

    trader_side = str(payload.get("trader_side", "")).upper()
    if trader_side == "MAKER":
        maker_orders = payload.get("maker_orders")
        if not isinstance(maker_orders, list):
            return None
        matched_size = 0.0
        weighted_notional = 0.0
        for item in maker_orders:
            if not isinstance(item, dict):
                continue
            if wallet_address is not None and _normalized_wallet_address(item.get("maker_address")) != wallet_address:
                continue
            if str(item.get("asset_id", "")) != token_id:
                continue
            if str(item.get("side", "")).upper() != side.upper():
                continue
            price = _to_optional_float(item.get("price"))
            size = _to_optional_float(item.get("matched_amount"))
            if price is None or size is None or size <= 0:
                continue
            if abs(price - preview_price) > 0.02:
                continue
            matched_size += size
            weighted_notional += size * price
        if matched_size <= 0:
            return None
        return TradeFillMatch(
            matched_size=round(matched_size, 6),
            fill_price=round(weighted_notional / matched_size, 6),
            fill_time=match_time,
        )

    if trader_side == "TAKER":
        if wallet_address is not None and _normalized_wallet_address(payload.get("maker_address")) != wallet_address:
            return None
        if str(payload.get("asset_id", "")) != token_id:
            return None
        if str(payload.get("side", "")).upper() != side.upper():
            return None
        price = _to_optional_float(payload.get("price"))
        size = _to_optional_float(payload.get("size"))
        if price is None or size is None or size <= 0:
            return None
        if abs(price - preview_price) > 0.02:
            return None
        return TradeFillMatch(
            matched_size=round(size, 6),
            fill_price=price,
            fill_time=match_time,
        )

    return None


def _normalized_wallet_address(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.lower()
