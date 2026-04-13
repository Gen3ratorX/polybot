from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from api.clob import build_clob_client
from api.catalyst import CatalystSnapshot
from api.gamma import GammaClient
from api.spot import SpotSnapshot
from bot.config import EnvironmentConfig, RuntimeConfig, resolve_execution_style
from bot.executor import LiveExitPreview, LiveOrderStatus, OrderExecutor
from bot.order_monitor import monitor_order_status
from bot.runtime_state import send_optional_alert, sync_live_state
from bot.tracker import TradeTracker
from models import OutcomeSide, Position, TradeOutcome


@dataclass(frozen=True, slots=True)
class ExitResult:
    position_id: int
    market_id: str
    order_id: str | None
    status: str
    exit_price: float | None
    matched_size: float
    pnl: float | None
    fully_closed: bool
    state_id: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "position_id": self.position_id,
            "market_id": self.market_id,
            "order_id": self.order_id,
            "status": self.status,
            "exit_price": self.exit_price,
            "matched_size": self.matched_size,
            "pnl": self.pnl,
            "fully_closed": self.fully_closed,
            "state_id": self.state_id,
        }


@dataclass(frozen=True, slots=True)
class _ExitOrderOutcome:
    preview: LiveExitPreview
    order_id: str
    submitted_at: datetime
    submit_response: dict[str, object]
    cancel_response: dict[str, object] | None
    final_status: LiveOrderStatus
    exit_reason: str


async def find_exit_candidate(
    tracker: TradeTracker,
    runtime: RuntimeConfig,
    spot_snapshot: SpotSnapshot | None = None,
    catalyst_snapshot: CatalystSnapshot | None = None,
) -> tuple[Position, object] | None:
    open_positions = tracker.list_open_positions(limit=max(1, runtime.execution.max_open_positions))
    if not open_positions:
        return None
    async with GammaClient() as gamma:
        for position in open_positions:
            market = await gamma.fetch_market(position.market_id)
            held_price = market.yes_price if position.side is OutcomeSide.YES else market.no_price
            estimated_proceeds = held_price * position.shares
            thesis_break = _should_thesis_break_exit(
                position,
                market,
                runtime,
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
            )
            take_profit = held_price >= runtime.execution.exit_target_price and estimated_proceeds > position.cost_basis
            if not thesis_break and not take_profit:
                continue
            return position, market
    return None


def _should_thesis_break_exit(
    position: Position,
    market: object,
    runtime: RuntimeConfig,
    *,
    as_of: datetime | None = None,
    spot_snapshot: SpotSnapshot | None = None,
    catalyst_snapshot: CatalystSnapshot | None = None,
) -> bool:
    if runtime.strategy.signal_mode == "momentum":
        return _should_momentum_thesis_break_exit(
            position,
            market,
            runtime,
            as_of=as_of,
            spot_snapshot=spot_snapshot,
            catalyst_snapshot=catalyst_snapshot,
        )
    rules = runtime.strategy.exit_rules
    reference = as_of or datetime.now(UTC)
    elapsed_minutes = max(0.0, (reference - position.timestamp).total_seconds() / 60)
    current_price = market.yes_price if position.side is OutcomeSide.YES else market.no_price
    entry_price = position.fill_price
    glide_minutes = max(1.0, float(rules.max_hold_minutes_without_progress))
    progress = min(elapsed_minutes / glide_minutes, 1.0)
    expected_price = min(1.0, entry_price + (1.0 - entry_price) * progress)
    stop_floor = expected_price * (1.0 - rules.stop_loss_pct)
    if current_price < stop_floor:
        return True
    if elapsed_minutes >= rules.max_hold_minutes_without_progress and current_price <= entry_price + 1e-9:
        return True
    return False


def _should_momentum_thesis_break_exit(
    position: Position,
    market: object,
    runtime: RuntimeConfig,
    *,
    as_of: datetime | None = None,
    spot_snapshot: SpotSnapshot | None = None,
    catalyst_snapshot: CatalystSnapshot | None = None,
) -> bool:
    rules = runtime.strategy.exit_rules
    reference = as_of or datetime.now(UTC)
    elapsed_minutes = max(0.0, (reference - position.timestamp).total_seconds() / 60)
    current_price = market.yes_price if position.side is OutcomeSide.YES else market.no_price
    entry_price = position.fill_price
    glide_minutes = max(1.0, float(rules.max_hold_minutes_without_progress))
    progress = min(elapsed_minutes / glide_minutes, 1.0)
    expected_price = min(1.0, entry_price + (1.0 - entry_price) * progress)
    stop_floor = expected_price * (1.0 - rules.stop_loss_pct)
    if current_price < stop_floor:
        return True
    if elapsed_minutes >= max(1.0, glide_minutes * 0.5) and current_price <= entry_price + 1e-9:
        return True
    if spot_snapshot is not None:
        if runtime.strategy.spot_max_age_seconds is not None and spot_snapshot.age_seconds > runtime.strategy.spot_max_age_seconds:
            return True
        if runtime.strategy.spot_min_abs_return_1h_pct is not None:
            long_move = abs(spot_snapshot.return_1h_pct or 0.0)
            if long_move < runtime.strategy.spot_min_abs_return_1h_pct and elapsed_minutes >= max(1.0, glide_minutes * 0.5):
                return True
        if (
            spot_snapshot.return_1h_pct is not None
            and spot_snapshot.return_15m_pct is not None
            and spot_snapshot.return_1h_pct * spot_snapshot.return_15m_pct < 0
        ):
            return True
    if catalyst_snapshot is not None and not catalyst_snapshot.active_events:
        if elapsed_minutes >= 5:
            return True
    return False


def try_auto_exit_position(
    *,
    env: EnvironmentConfig,
    runtime: RuntimeConfig,
    tracker: TradeTracker,
    monitor_seconds: int,
    poll_interval: float,
    cancel_if_open: bool,
    spot_snapshot: SpotSnapshot | None = None,
    catalyst_snapshot: CatalystSnapshot | None = None,
) -> ExitResult | None:
    if catalyst_snapshot is None:
        candidate = asyncio.run(find_exit_candidate(tracker, runtime, spot_snapshot=spot_snapshot))
    else:
        candidate = asyncio.run(
            find_exit_candidate(
                tracker,
                runtime,
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
            )
        )
    if candidate is None:
        return None
    position, market = candidate
    client = build_clob_client(env, include_api_creds=True)
    executor = OrderExecutor(client, execution_style=resolve_execution_style(runtime))
    exit_reason = _auto_exit_reason(
        position,
        market,
        runtime,
        spot_snapshot=spot_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )
    realized_pnl_total = 0.0
    first_outcome = _submit_exit_order(
        env=env,
        runtime=runtime,
        tracker=tracker,
        executor=executor,
        market=market,
        position=position,
        monitor_seconds=monitor_seconds,
        poll_interval=poll_interval,
        cancel_if_open=cancel_if_open,
        exit_reason=exit_reason,
        request_size=position.shares,
    )

    if first_outcome.final_status.matched_size <= 0:
        send_optional_alert(
            env,
            (
                f"Auto-exit order finished unfilled for {position.market_question}\n"
                f"Order ID: {first_outcome.order_id}\n"
                f"Status: {first_outcome.final_status.status}"
            ),
            level="WARN",
        )
        return ExitResult(
            position_id=position.position_id or 0,
            market_id=position.market_id,
            order_id=first_outcome.order_id,
            status=first_outcome.final_status.status,
            exit_price=first_outcome.final_status.price,
            matched_size=0.0,
            pnl=None,
            fully_closed=False,
            state_id=None,
        )

    filled_shares = round(first_outcome.final_status.matched_size, 6)
    if abs(filled_shares - position.shares) <= 0.0001:
        return _finalize_closed_position(
            env=env,
            runtime=runtime,
            tracker=tracker,
            client=client,
            position=position,
            order_id=first_outcome.order_id,
            status=first_outcome.final_status,
            limit_price=first_outcome.preview.limit_price,
            realized_pnl_carry=realized_pnl_total,
        )

    first_exit_price = first_outcome.final_status.price or first_outcome.preview.limit_price
    realized_pnl_total = round(
        realized_pnl_total + (filled_shares * (first_exit_price - position.fill_price)),
        6,
    )
    partial_result = _apply_partial_exit_fill(
        tracker=tracker,
        position=position,
        order_id=first_outcome.order_id,
        status=first_outcome.final_status,
        exit_reason=exit_reason,
    )
    remaining_position = partial_result["position"]
    remaining_shares = float(partial_result["remaining_shares"])
    min_order_size = _exit_min_order_size(
        executor=executor,
        token_id=market.token_id_for(position.side),
    )

    if remaining_position.shares <= 0:
        return _finalize_closed_position(
            env=env,
            runtime=runtime,
            tracker=tracker,
            client=client,
            position=position,
            order_id=first_outcome.order_id,
            status=first_outcome.final_status,
            limit_price=first_outcome.preview.limit_price,
            realized_pnl_carry=realized_pnl_total,
        )

    if min_order_size is None or remaining_shares + 1e-9 < min_order_size:
        send_optional_alert(
            env,
            (
                f"Auto-exit partial fill requires review for {position.market_question}\n"
                f"Order ID: {first_outcome.order_id}\n"
                f"Matched shares: {filled_shares:.4f}/{position.shares:.4f}\n"
                f"Remaining shares: {remaining_shares:.4f}"
            ),
            level="KILL",
        )
        return ExitResult(
            position_id=remaining_position.position_id or position.position_id or 0,
            market_id=remaining_position.market_id,
            order_id=first_outcome.order_id,
            status=first_outcome.final_status.status,
            exit_price=first_outcome.final_status.price,
            matched_size=filled_shares,
            pnl=None,
            fully_closed=False,
            state_id=None,
        )

    second_outcome = _submit_exit_order(
        env=env,
        runtime=runtime,
        tracker=tracker,
        executor=executor,
        market=market,
        position=remaining_position,
        monitor_seconds=monitor_seconds,
        poll_interval=poll_interval,
        cancel_if_open=cancel_if_open,
        exit_reason=exit_reason,
        request_size=remaining_position.shares,
    )

    if second_outcome.final_status.matched_size <= 0:
        send_optional_alert(
            env,
            (
                f"Auto-exit follow-up order finished unfilled for {position.market_question}\n"
                f"Order ID: {second_outcome.order_id}\n"
                f"Status: {second_outcome.final_status.status}"
            ),
            level="WARN",
        )
        return ExitResult(
            position_id=remaining_position.position_id or position.position_id or 0,
            market_id=remaining_position.market_id,
            order_id=second_outcome.order_id,
            status=second_outcome.final_status.status,
            exit_price=second_outcome.final_status.price,
            matched_size=filled_shares,
            pnl=None,
            fully_closed=False,
            state_id=None,
        )

    second_filled = round(second_outcome.final_status.matched_size, 6)
    second_exit_price = second_outcome.final_status.price or second_outcome.preview.limit_price
    realized_pnl_total = round(
        realized_pnl_total + (second_filled * (second_exit_price - remaining_position.fill_price)),
        6,
    )
    if abs(second_filled - remaining_position.shares) <= 0.0001:
        return _finalize_closed_position(
            env=env,
            runtime=runtime,
            tracker=tracker,
            client=client,
            position=remaining_position,
            order_id=second_outcome.order_id,
            status=second_outcome.final_status,
            limit_price=second_outcome.preview.limit_price,
            realized_pnl_carry=realized_pnl_total,
        )

    tail_partial = _apply_partial_exit_fill(
        tracker=tracker,
        position=remaining_position,
        order_id=second_outcome.order_id,
        status=second_outcome.final_status,
        exit_reason=exit_reason,
    )
    tail_position = tail_partial["position"]
    tail_filled = filled_shares + second_filled
    send_optional_alert(
        env,
        (
            f"Auto-exit partial fill requires review for {position.market_question}\n"
            f"Order ID: {second_outcome.order_id}\n"
            f"Matched shares: {second_filled:.4f}/{remaining_position.shares:.4f}\n"
            f"Remaining shares: {float(tail_partial['remaining_shares']):.4f}"
        ),
        level="KILL",
    )
    return ExitResult(
        position_id=tail_position.position_id or position.position_id or 0,
        market_id=tail_position.market_id,
        order_id=second_outcome.order_id,
        status=second_outcome.final_status.status,
        exit_price=second_outcome.final_status.price,
        matched_size=tail_filled,
        pnl=None,
        fully_closed=False,
        state_id=None,
    )


def _submit_exit_order(
    *,
    env: EnvironmentConfig,
    runtime: RuntimeConfig,
    tracker: TradeTracker,
    executor: OrderExecutor,
    market: object,
    position: Position,
    monitor_seconds: int,
    poll_interval: float,
    cancel_if_open: bool,
    exit_reason: str,
    request_size: float,
) -> _ExitOrderOutcome:
    preview = executor.preview_limit_sell(
        market,
        side=position.side,
        shares=request_size,
    )
    submitted_at = datetime.now(UTC)
    submit_response = executor.place_limit_sell(preview)
    order_id = executor.extract_order_id(submit_response)
    if not order_id:
        raise ValueError("Could not extract exit order id from submit response")

    tracker.record_order(
        order_id=order_id,
        market_id=position.market_id,
        strategy_name=position.strategy_name,
        status="OPEN",
        requested_size=request_size,
        limit_price=preview.limit_price,
        filled_size=0.0,
        last_seen_status="OPEN",
        last_seen_at=submitted_at,
        exchange_payload={
            "exit_reason": exit_reason,
            "position_id": position.position_id,
            "market_question": position.market_question,
            "preview": _preview_payload(preview),
        },
    )

    send_optional_alert(
        env,
        (
            f"Auto-exit order submitted for {position.market_question}\n"
            f"Order ID: {order_id}\n"
            f"Side: {position.side.value}\n"
            f"Shares: {request_size:.4f}\n"
            f"Limit price: {preview.limit_price:.3f}"
        ),
        level="INFO",
    )

    final_status = monitor_order_status(
        env=env,
        executor=executor,
        order_id=order_id,
        market_id=market.condition_id,
        timeout_seconds=monitor_seconds,
        poll_interval=poll_interval,
        heartbeat_seconds=runtime.execution.websocket_heartbeat_seconds,
    )
    cancel_response = None
    if not final_status.is_terminal and cancel_if_open:
        cancel_response = executor.cancel_order(order_id)
        final_status = executor.get_order_status(order_id)
    final_status = executor.reconcile_fill_status(
        preview,
        final_status,
        submitted_at=submitted_at,
        side="SELL",
    )
    _persist_exit_order_state(
        tracker=tracker,
        position=position,
        preview=preview,
        exit_reason=exit_reason,
        final_status=final_status,
        submitted_at=submitted_at,
    )

    return _ExitOrderOutcome(
        preview=preview,
        order_id=order_id,
        submitted_at=submitted_at,
        submit_response=submit_response,
        cancel_response=cancel_response,
        final_status=final_status,
        exit_reason=exit_reason,
    )


def _persist_exit_order_state(
    *,
    tracker: TradeTracker,
    position: Position,
    preview: LiveExitPreview,
    exit_reason: str,
    final_status: LiveOrderStatus,
    submitted_at: datetime,
) -> None:
    payload = {
        "exit_reason": exit_reason,
        "position_id": position.position_id,
        "market_id": position.market_id,
        "market_question": position.market_question,
        "preview": _preview_payload(preview),
        "final_status": {
            "order_id": final_status.order_id,
            "status": final_status.status,
            "matched_size": final_status.matched_size,
            "original_size": final_status.original_size,
            "price": final_status.price,
        },
    }
    if final_status.has_fill:
        tracker.update_order_fill(
            order_id=final_status.order_id,
            market_id=position.market_id,
            strategy_name=position.strategy_name,
            requested_size=preview.shares,
            limit_price=preview.limit_price,
            filled_size=final_status.matched_size,
            last_seen_status=final_status.status,
            last_seen_at=final_status.created_at or submitted_at,
            exchange_payload=payload,
        )
    elif final_status.is_terminal:
        tracker.close_order(
            order_id=final_status.order_id,
            strategy_name=position.strategy_name,
            status=final_status.status,
            last_seen_status=final_status.status,
            last_seen_at=final_status.created_at or submitted_at,
            filled_size=final_status.matched_size,
            exchange_payload=payload,
        )
    else:
        tracker.record_order(
            order_id=final_status.order_id,
            market_id=position.market_id,
            strategy_name=position.strategy_name,
            status=final_status.status,
            requested_size=preview.shares,
            limit_price=preview.limit_price,
            filled_size=final_status.matched_size,
            last_seen_status=final_status.status,
            last_seen_at=final_status.created_at or submitted_at,
            exchange_payload=payload,
        )


def _apply_partial_exit_fill(
    *,
    tracker: TradeTracker,
    position: Position,
    order_id: str,
    status: LiveOrderStatus,
    exit_reason: str,
) -> dict[str, object]:
    filled_shares = round(max(status.matched_size, 0.0), 6)
    remaining_shares = round(max(position.shares - filled_shares, 0.0), 6)
    remaining_cost_basis = round(
        max(position.cost_basis - (position.fill_price * filled_shares), 0.0),
        6,
    )
    remaining_position = replace(
        position,
        shares=remaining_shares,
        cost_basis=remaining_cost_basis,
    )
    tracker.upsert_position(
        remaining_position,
        notes=(
            f"partial_exit_order_id={order_id}; "
            f"exit_reason={exit_reason}; "
            f"filled_shares={filled_shares:.6f}; "
            f"remaining_shares={remaining_shares:.6f}"
        ),
    )
    return {
        "position": remaining_position,
        "filled_shares": filled_shares,
        "remaining_shares": remaining_shares,
    }


def _finalize_closed_position(
    *,
    env: EnvironmentConfig,
    runtime: RuntimeConfig,
    tracker: TradeTracker,
    client,
    position: Position,
    order_id: str,
    status: LiveOrderStatus,
    limit_price: float,
    realized_pnl_carry: float = 0.0,
) -> ExitResult:
    exit_price = status.price or limit_price
    resolved_at = status.created_at or datetime.now(UTC)
    closed_position = position.settle(
        resolution_price=exit_price,
        resolved_at=resolved_at,
    )
    total_pnl = round((closed_position.pnl or 0.0) + realized_pnl_carry, 6)
    tracker.upsert_position(closed_position, notes=f"auto_exit_order_id={order_id}")
    if position.order_id:
        outcome = TradeOutcome.WIN if total_pnl > 0 else TradeOutcome.LOSS
        tracker.update_trade_resolution(
            order_id=position.order_id,
            outcome=outcome,
            resolution_price=exit_price,
            pnl=total_pnl,
            resolved_at=resolved_at,
        )
    state_sync = sync_live_state(
        tracker=tracker,
        runtime=runtime,
        env=env,
        clob_client=client,
    )
    send_optional_alert(
        env,
        (
            f"Auto-exit closed {position.market_question}\n"
            f"Order ID: {order_id}\n"
            f"Exit price: {exit_price:.3f}\n"
            f"PnL: ${total_pnl:.2f}"
        ),
        level="WIN" if total_pnl > 0 else "WARN",
    )
    return ExitResult(
        position_id=closed_position.position_id or 0,
        market_id=closed_position.market_id,
        order_id=order_id,
        status=status.status,
        exit_price=exit_price,
        matched_size=status.matched_size,
        pnl=total_pnl,
        fully_closed=True,
        state_id=state_sync.state_id,
    )


def _exit_min_order_size(*, executor: OrderExecutor, token_id: str) -> float | None:
    snapshot = executor.get_order_book_snapshot(token_id)
    if snapshot.min_order_size is not None:
        return float(snapshot.min_order_size)
    return None


def _auto_exit_reason(
    position: Position,
    market: object,
    runtime: RuntimeConfig,
    *,
    as_of: datetime | None = None,
    spot_snapshot: SpotSnapshot | None = None,
    catalyst_snapshot: CatalystSnapshot | None = None,
) -> str:
    current_price = market.yes_price if position.side is OutcomeSide.YES else market.no_price
    estimated_proceeds = current_price * position.shares
    take_profit = current_price >= runtime.execution.exit_target_price and estimated_proceeds > position.cost_basis
    if take_profit:
        return "take_profit"
    if _should_thesis_break_exit(
        position,
        market,
        runtime,
        as_of=as_of,
        spot_snapshot=spot_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    ):
        return "thesis_break"
    return "auto_exit"


def _preview_payload(preview: LiveExitPreview) -> dict[str, object]:
    as_dict = getattr(preview, "as_dict", None)
    if callable(as_dict):
        payload = as_dict()
        if isinstance(payload, dict):
            return payload
    if hasattr(preview, "__dict__"):
        return dict(vars(preview))
    return {
        "market_id": getattr(preview, "market_id", None),
        "question": getattr(preview, "question", None),
        "outcome_side": getattr(preview, "outcome_side", None),
        "token_id": getattr(preview, "token_id", None),
        "shares": getattr(preview, "shares", None),
        "limit_price": getattr(preview, "limit_price", None),
    }
