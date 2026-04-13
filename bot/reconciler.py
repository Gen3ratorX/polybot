from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from api.gamma import GammaClient
from bot.executor import LiveOrderStatus, OrderExecutor
from bot.tracker import TradeTracker
from models import OutcomeSide, Position, Trade, TradeOutcome
from py_clob_client.clob_types import TradeParams


@dataclass(frozen=True, slots=True)
class PositionReconcileResult:
    position_id: int
    order_id: str | None
    market_id: str
    resolution_price: float
    pnl: float

    @property
    def outcome(self) -> TradeOutcome:
        return TradeOutcome.WIN if self.pnl > 0 else TradeOutcome.LOSS


@dataclass(frozen=True, slots=True)
class OrderReconcileResult:
    order_id: str
    market_id: str
    previous_status: str
    current_status: str
    requested_size: float
    filled_size: float
    provisional_cancel_corrected: bool
    created_position: bool


async def reconcile_open_orders(
    tracker: TradeTracker,
    gamma: GammaClient,
    clob_client: Any,
    *,
    limit: int = 100,
) -> list[OrderReconcileResult]:
    order_rows = tracker.list_orders(
        limit=limit,
        statuses=("OPEN", "LIVE_RESTING", "PARTIALLY_FILLED", "CANCELLED", "REJECTED", "EXPIRED"),
    )
    results: list[OrderReconcileResult] = []
    seen_order_ids: set[str] = set()
    for order in order_rows:
        order_id = str(order["order_id"])
        if order_id in seen_order_ids:
            continue
        seen_order_ids.add(order_id)

        exchange_status = _fetch_exchange_order_status(clob_client, order_id)
        if exchange_status is None:
            continue

        requested_size = float(order["requested_size"])
        limit_price = float(order["limit_price"]) if order.get("limit_price") is not None else None
        existing_status = str(order["status"])
        resolved_status = exchange_status.status
        resolved_fill = exchange_status.matched_size
        resolved_price = exchange_status.price
        resolved_time = exchange_status.created_at or _coerce_datetime(order["last_seen_at"])
        if resolved_time is None:
            resolved_time = datetime.now(UTC)

        history_fill = _match_order_fill_from_history(
            clob_client,
            exchange_status,
            submitted_at=_coerce_datetime(order["last_seen_at"]) or _coerce_datetime(order["timestamp"]) or resolved_time,
        )
        if history_fill is not None and history_fill[2] > resolved_fill + 1e-9:
            resolved_time, resolved_price, resolved_fill = history_fill

        provisional_cancel_corrected = (
            existing_status in {"CANCELLED", "REJECTED", "EXPIRED"} and resolved_fill > 0
        )
        created_position = False

        if resolved_fill > 0:
            tracker.update_order_fill(
                order_id=order_id,
                market_id=order["market_id"],
                strategy_name=order["strategy_name"],
                requested_size=requested_size,
                limit_price=limit_price,
                filled_size=resolved_fill,
                last_seen_status=resolved_status,
                last_seen_at=resolved_time,
                exchange_payload=exchange_status_payload(exchange_status),
            )
            if exchange_status.side == "BUY" and tracker.get_position_by_order_id(order_id) is None:
                market = await gamma.fetch_market(order["market_id"])
                outcome_side = _outcome_side_for_token(market, exchange_status.token_id)
                trade = Trade(
                    timestamp=resolved_time,
                    market_id=market.market_id,
                    market_question=market.question,
                    category=market.category,
                    side=outcome_side,
                    entry_price=limit_price or resolved_price or exchange_status.price or 0.0,
                    screened_price=limit_price or resolved_price or exchange_status.price or 0.0,
                    position_size=round(resolved_fill * (resolved_price or exchange_status.price or 0.0), 6),
                    order_id=order_id,
                    fill_price=resolved_price or exchange_status.price,
                    fill_time=resolved_time,
                    outcome=TradeOutcome.PENDING,
                    paper_trade=False,
                    strategy_name=order["strategy_name"],
                )
                tracker.record_trade(
                    trade,
                    notes=json.dumps(
                        {
                            "reconciled_from_order": {
                                "order_id": order_id,
                                "exchange_status": exchange_status.status,
                                "matched_size": resolved_fill,
                                "price": resolved_price,
                            }
                        }
                    ),
                )
                tracker.register_open_position_from_trade(trade)
                created_position = True
        elif resolved_status in {"CANCELLED", "REJECTED", "EXPIRED"}:
            tracker.close_order(
                order_id=order_id,
                strategy_name=order["strategy_name"],
                status=resolved_status,
                last_seen_status=resolved_status,
                last_seen_at=resolved_time,
                filled_size=resolved_fill,
                exchange_payload=exchange_status_payload(exchange_status),
            )
        else:
            tracker.record_order(
                order_id=order_id,
                market_id=order["market_id"],
                strategy_name=order["strategy_name"],
                status=resolved_status,
                requested_size=requested_size,
                limit_price=limit_price,
                filled_size=resolved_fill,
                last_seen_status=resolved_status,
                last_seen_at=resolved_time,
                exchange_payload=exchange_status_payload(exchange_status),
            )

        results.append(
            OrderReconcileResult(
                order_id=order_id,
                market_id=str(order["market_id"]),
                previous_status=existing_status,
                current_status=resolved_status if resolved_fill <= 0 else ("FILLED" if resolved_fill + 1e-9 >= requested_size else "PARTIALLY_FILLED"),
                requested_size=requested_size,
                filled_size=resolved_fill,
                provisional_cancel_corrected=provisional_cancel_corrected,
                created_position=created_position,
            )
        )
    return results


async def reconcile_open_positions(
    tracker: TradeTracker,
    gamma: GammaClient,
    *,
    limit: int = 100,
) -> list[PositionReconcileResult]:
    open_positions = tracker.list_open_positions(limit=limit)
    results: list[PositionReconcileResult] = []
    for position in open_positions:
        market = await gamma.fetch_market(position.market_id)
        resolution_price = market.resolution_price_for(position.side)
        if resolution_price is None:
            continue
        resolved_at = datetime.now(UTC)
        closed = position.settle(resolution_price=resolution_price, resolved_at=resolved_at)
        tracker.upsert_position(closed)
        if position.order_id:
            outcome = TradeOutcome.WIN if (closed.pnl or 0.0) > 0 else TradeOutcome.LOSS
            tracker.update_trade_resolution(
                order_id=position.order_id,
                outcome=outcome,
                resolution_price=resolution_price,
                pnl=closed.pnl or 0.0,
                resolved_at=resolved_at,
            )
        results.append(
            PositionReconcileResult(
                position_id=closed.position_id or 0,
                order_id=closed.order_id,
                market_id=closed.market_id,
                resolution_price=resolution_price,
                pnl=closed.pnl or 0.0,
            )
        )
    return results


async def reconcile_manual_exits(
    tracker: TradeTracker,
    gamma: GammaClient,
    *,
    clob_client,
    wallet_address: str | None,
    limit: int = 100,
) -> list[PositionReconcileResult]:
    open_positions = tracker.list_open_positions(limit=limit)
    if not open_positions:
        return []

    earliest_opened = min(position.timestamp for position in open_positions)
    try:
        trades = clob_client.get_trades(
            __import__("py_clob_client.clob_types", fromlist=["TradeParams"]).TradeParams(
                after=max(0, int(earliest_opened.timestamp()) - 300)
            )
        )
    except Exception:
        return []
    if not isinstance(trades, list):
        return []

    normalized_wallet = _normalized_wallet(wallet_address)
    results: list[PositionReconcileResult] = []
    for position in open_positions:
        market = await gamma.fetch_market(position.market_id)
        token_id = market.token_id_for(position.side)
        match = _match_manual_exit(
            trades,
            token_id=token_id,
            shares=position.shares,
            opened_at=position.timestamp,
            wallet_address=normalized_wallet,
        )
        if match is None:
            continue
        resolved_at, exit_price, matched_size = match
        if matched_size + 0.0001 < position.shares:
            continue
        closed = position.settle(resolution_price=exit_price, resolved_at=resolved_at)
        tracker.upsert_position(closed, notes="manual exit detected from live trade history")
        if position.order_id:
            outcome = TradeOutcome.WIN if (closed.pnl or 0.0) > 0 else TradeOutcome.LOSS
            tracker.update_trade_resolution(
                order_id=position.order_id,
                outcome=outcome,
                resolution_price=exit_price,
                pnl=closed.pnl or 0.0,
                resolved_at=resolved_at,
            )
        results.append(
            PositionReconcileResult(
                position_id=closed.position_id or 0,
                order_id=closed.order_id,
                market_id=closed.market_id,
                resolution_price=exit_price,
                pnl=closed.pnl or 0.0,
            )
        )
    return results


def _match_manual_exit(
    trades: list[dict[str, object]],
    *,
    token_id: str,
    shares: float,
    opened_at: datetime,
    wallet_address: str | None,
) -> tuple[datetime, float, float] | None:
    matched_size = 0.0
    weighted_notional = 0.0
    exit_time: datetime | None = None
    for payload in trades:
        top = _extract_trade_fill(
            payload,
            token_id=token_id,
            side="SELL",
            wallet_address=wallet_address,
            opened_at=opened_at,
        )
        if top is None:
            continue
        fill_time, price, size = top
        matched_size += size
        weighted_notional += size * price
        if exit_time is None or fill_time > exit_time:
            exit_time = fill_time
        if matched_size + 0.0001 >= shares:
            break
    if matched_size <= 0 or exit_time is None:
        return None
    return exit_time, round(weighted_notional / matched_size, 6), round(matched_size, 6)


def _extract_trade_fill(
    payload: dict[str, object],
    *,
    token_id: str,
    side: str,
    wallet_address: str | None,
    opened_at: datetime,
) -> tuple[datetime, float, float] | None:
    match_time = _parse_trade_time(payload.get("match_time") or payload.get("last_update"))
    if match_time is None or match_time < opened_at:
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
            if wallet_address is not None and _normalized_wallet(item.get("maker_address")) != wallet_address:
                continue
            if str(item.get("asset_id", "")) != token_id:
                continue
            if str(item.get("side", "")).upper() != side:
                continue
            price = _to_float(item.get("price"))
            size = _to_float(item.get("matched_amount"))
            if price is None or size is None or size <= 0:
                continue
            matched_size += size
            weighted_notional += size * price
        if matched_size <= 0:
            return None
        return match_time, round(weighted_notional / matched_size, 6), round(matched_size, 6)

    if wallet_address is not None and _normalized_wallet(payload.get("maker_address")) != wallet_address:
        return None
    if str(payload.get("asset_id", "")) != token_id:
        return None
    if str(payload.get("side", "")).upper() != side:
        return None
    price = _to_float(payload.get("price"))
    size = _to_float(payload.get("size"))
    if price is None or size is None or size <= 0:
        return None
    return match_time, price, size


def _parse_trade_time(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(str(value)), tz=UTC)
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _normalized_wallet(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.lower()


def _fetch_exchange_order_status(clob_client: Any, order_id: str) -> LiveOrderStatus | None:
    try:
        if hasattr(clob_client, "get_order_status"):
            payload = clob_client.get_order_status(order_id)
            if isinstance(payload, LiveOrderStatus):
                return payload
            if isinstance(payload, dict):
                return OrderExecutor.parse_order_status(payload)
        if hasattr(clob_client, "get_order"):
            payload = clob_client.get_order(order_id)
            if isinstance(payload, dict):
                return OrderExecutor.parse_order_status(payload)
    except Exception:
        return None
    return None


def _match_order_fill_from_history(
    clob_client: Any,
    status: LiveOrderStatus,
    *,
    submitted_at: datetime,
) -> tuple[datetime, float, float] | None:
    token_id = status.token_id
    side = status.side
    if token_id is None or side is None:
        return None
    try:
        trades = clob_client.get_trades(
            TradeParams(
                asset_id=token_id,
                after=max(0, int(submitted_at.timestamp()) - 300),
            )
        )
    except Exception:
        return None
    if not isinstance(trades, list):
        return None

    matched_size = 0.0
    weighted_notional = 0.0
    fill_time: datetime | None = None
    for payload in trades:
        candidate = _extract_trade_fill(
            payload,
            token_id=token_id,
            side=side,
            wallet_address=_normalized_wallet(getattr(clob_client, "funder", None)),
            opened_at=submitted_at,
        )
        if candidate is None:
            continue
        candidate_time, candidate_price, candidate_size = candidate
        matched_size += candidate_size
        weighted_notional += candidate_size * candidate_price
        if fill_time is None or candidate_time > fill_time:
            fill_time = candidate_time
    if matched_size <= 0 or fill_time is None:
        return None
    return fill_time, round(weighted_notional / matched_size, 6), round(matched_size, 6)


def exchange_status_payload(status: LiveOrderStatus) -> dict[str, object]:
    return {
        "id": status.order_id,
        "status": status.status,
        "created_at": None if status.created_at is None else status.created_at.isoformat(),
        "market_id": status.market_condition_id,
        "asset_id": status.token_id,
        "side": status.side,
        "price": status.price,
        "original_size": status.original_size,
        "size_matched": status.matched_size,
    }


def _outcome_side_for_token(market, token_id: str | None) -> OutcomeSide:
    if token_id is None:
        return OutcomeSide.YES
    if token_id == market.yes_token_id:
        return OutcomeSide.YES
    if token_id == market.no_token_id:
        return OutcomeSide.NO
    return OutcomeSide.YES
