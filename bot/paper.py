from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from uuid import uuid4
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from bot.ranker import EdgeRanker, RankedMarket
from bot.risk import KillSignal, RiskManager
from api.catalyst import CatalystSnapshot
from api.spot import SpotSnapshot
from bot.tracker import TradeTracker
from models import BotState, Market, OutcomeSide, Position, PositionStatus, Trade


class ScannerProtocol(Protocol):
    async def scan(self, *, as_of: datetime | None = None) -> list: ...


class ResolverProtocol(Protocol):
    def __call__(self, ranked_market: RankedMarket) -> float: ...


class PaperFillMode(str, Enum):
    FULL_FILL = "FULL_FILL"
    PARTIAL_FILL = "PARTIAL_FILL"
    NO_FILL = "NO_FILL"


@dataclass(frozen=True, slots=True)
class PaperTradeCycleResult:
    trade: Trade | None
    kill_signal: KillSignal | None
    state: BotState


@dataclass(frozen=True, slots=True)
class PaperExecutionResult:
    mode: PaperFillMode
    live_status: str
    ledger_status: str
    fill_ratio: float
    filled_size_usdc: float
    resting_size_usdc: float


@dataclass(frozen=True, slots=True)
class PaperExecutionSimulator:
    mode: PaperFillMode = PaperFillMode.PARTIAL_FILL
    partial_fill_ratio: float = 0.5

    def simulate(self, *, requested_size_usdc: float) -> PaperExecutionResult:
        if requested_size_usdc <= 0:
            raise ValueError("requested_size_usdc must be positive")

        if self.mode is PaperFillMode.FULL_FILL:
            fill_ratio = 1.0
            live_status = "FILLED"
            ledger_status = "FILLED"
        elif self.mode is PaperFillMode.NO_FILL:
            fill_ratio = 0.0
            live_status = "LIVE_RESTING"
            ledger_status = "OPEN"
        else:
            fill_ratio = max(0.0, min(1.0, self.partial_fill_ratio))
            if fill_ratio <= 0:
                live_status = "LIVE_RESTING"
                ledger_status = "OPEN"
            elif fill_ratio >= 1:
                live_status = "FILLED"
                ledger_status = "FILLED"
            else:
                live_status = "PARTIALLY_FILLED"
                ledger_status = "PARTIALLY_FILLED"

        filled_size_usdc = round(requested_size_usdc * fill_ratio, 6)
        resting_size_usdc = round(max(0.0, requested_size_usdc - filled_size_usdc), 6)
        return PaperExecutionResult(
            mode=self.mode,
            live_status=live_status,
            ledger_status=ledger_status,
            fill_ratio=round(fill_ratio, 6),
            filled_size_usdc=filled_size_usdc,
            resting_size_usdc=resting_size_usdc,
        )


@dataclass(frozen=True, slots=True)
class _PaperOpenPosition:
    position: Position
    entry_market: Market
    opened_at: datetime
    close_after: datetime
    order_id: str


class PaperTradingEngine:
    def __init__(
        self,
        *,
        scanner: ScannerProtocol,
        ranker: EdgeRanker,
        tracker: TradeTracker,
        risk_manager: RiskManager,
        initial_bankroll: float,
        trade_size_usd: float = 1.0,
        strategy_name: str | None = None,
        resolver: ResolverProtocol | None = None,
        execution_simulator: PaperExecutionSimulator | None = None,
        settlement_delay_minutes: int = 0,
        session_id: str | None = None,
        open_position_alert: Callable[[str], object] | None = None,
    ) -> None:
        if initial_bankroll <= 0:
            raise ValueError("initial_bankroll must be positive")
        if trade_size_usd <= 0:
            raise ValueError("trade_size_usd must be positive")
        if settlement_delay_minutes < 0:
            raise ValueError("settlement_delay_minutes must be non-negative")

        start = datetime.now(UTC)
        self.scanner = scanner
        self.ranker = ranker
        self.tracker = tracker
        self.risk_manager = risk_manager
        self.trade_size_usd = trade_size_usd
        self.strategy_name = strategy_name
        self.session_id = session_id
        self.resolver = resolver or default_resolver
        self.execution_simulator = execution_simulator or PaperExecutionSimulator()
        self.settlement_delay_minutes = settlement_delay_minutes
        self.open_position_alert = open_position_alert
        self._open_positions: dict[str, _PaperOpenPosition] = {}
        self.state = BotState(
            timestamp=start,
            bankroll=initial_bankroll,
            phase=0,
            bankroll_start_of_day=initial_bankroll,
            bankroll_start_of_week=initial_bankroll,
            strategy_name=strategy_name,
        )

    async def run_cycle(
        self,
        *,
        as_of: datetime | None = None,
        spot_snapshot: SpotSnapshot | dict[str, SpotSnapshot] | None = None,
        catalyst_snapshot: CatalystSnapshot | None = None,
        blocked_market_ids: set[str] | None = None,
        market_universe: Sequence[Market] | Mapping[str, Market] | None = None,
        drain_only: bool = False,
    ) -> PaperTradeCycleResult:
        timestamp = as_of or datetime.now(UTC)
        current_state = replace(self.state, timestamp=timestamp)

        kill_signal = self.risk_manager.check_all_kills(current_state)
        if kill_signal is not None:
            paused_state = replace(
                current_state,
                is_paused=True,
                pause_level=kill_signal.level,
                pause_reason=kill_signal.reason,
                pause_until=(
                    None
                    if kill_signal.pause_minutes < 0
                    else timestamp + timedelta(minutes=kill_signal.pause_minutes)
                ),
            )
            self.tracker.record_state(paused_state, session_id=self.session_id)
            self.state = paused_state
            return PaperTradeCycleResult(trade=None, kill_signal=kill_signal, state=paused_state)

        market_lookup = _normalize_market_universe(market_universe) if market_universe is not None else {}
        if market_universe is not None:
            candidates = _filter_market_universe(
                self.scanner,
                list(market_lookup.values()),
                as_of=timestamp,
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
            )
        else:
            try:
                candidates = await self.scanner.scan(
                    as_of=timestamp,
                    spot_snapshot=spot_snapshot,
                    catalyst_snapshot=catalyst_snapshot,
                )
            except TypeError as exc:
                if "spot_snapshot" not in str(exc) and "catalyst_snapshot" not in str(exc):
                    raise
                candidates = await self.scanner.scan(as_of=timestamp)
            market_lookup = {market.market_id: market for market in candidates}

        if not market_lookup:
            market_lookup = {market.market_id: market for market in candidates}

        settled_trade, settled_bankroll_delta = self._settle_due_positions(
            timestamp=timestamp,
            market_lookup=market_lookup,
        )
        if settled_trade is not None:
            current_state = replace(
                current_state,
                bankroll=round(current_state.bankroll + settled_bankroll_delta, 6),
                daily_pnl=round(current_state.daily_pnl + (settled_trade.pnl or 0.0), 6),
                weekly_pnl=round(current_state.weekly_pnl + (settled_trade.pnl or 0.0), 6),
                total_trades=current_state.total_trades + 1,
                recent_trades=(*current_state.recent_trades, settled_trade),
                open_positions=self.tracker.open_position_count(self.strategy_name, session_id=self.session_id),
            )
            self.tracker.record_trade(settled_trade)
            self.tracker.record_state(current_state, session_id=self.session_id)

        if drain_only:
            open_orders = self.tracker.open_order_count(self.strategy_name, session_id=self.session_id)
            open_positions = self.tracker.open_position_count(self.strategy_name, session_id=self.session_id)
            drained_state = replace(
                current_state,
                open_orders=open_orders,
                open_positions=open_positions,
                strategy_name=self.strategy_name,
            )
            self.tracker.record_state(drained_state, session_id=self.session_id)
            self.state = drained_state
            return PaperTradeCycleResult(trade=settled_trade, kill_signal=None, state=drained_state)

        ranked = self.ranker.rank_markets(candidates, as_of=timestamp)
        blocked_ids = set(blocked_market_ids or ())
        blocked_ids.update(self._open_positions.keys())
        if blocked_ids:
            ranked = [item for item in ranked if item.market.market_id not in blocked_ids]
        if not ranked:
            self.state = current_state
            return PaperTradeCycleResult(trade=settled_trade, kill_signal=None, state=current_state)

        selected = ranked[0]
        position_size = min(self.trade_size_usd, current_state.bankroll)
        execution = self.execution_simulator.simulate(requested_size_usdc=position_size)
        order_id = _paper_order_id(selected.market.market_id, timestamp)
        self.tracker.record_order(
            order_id=order_id,
            market_id=selected.market.market_id,
            strategy_name=self.strategy_name,
            session_id=self.session_id,
            status=execution.ledger_status,
            requested_size=position_size,
            limit_price=selected.selected_price,
            filled_size=execution.filled_size_usdc,
            last_seen_status=execution.live_status,
            last_seen_at=timestamp,
            exchange_payload={
                "mode": execution.mode.value,
                "fill_ratio": execution.fill_ratio,
                "live_status": execution.live_status,
                "ledger_status": execution.ledger_status,
                "resting_size_usdc": execution.resting_size_usdc,
            },
        )

        resolved_trade = settled_trade
        bankroll_delta = settled_bankroll_delta
        updated_trades = current_state.recent_trades if settled_trade is None else (*current_state.recent_trades, settled_trade)
        if execution.filled_size_usdc > 0:
            open_position = Position(
                timestamp=timestamp,
                market_id=selected.market.market_id,
                market_question=selected.market.question,
                category=selected.market.category,
                side=selected.selected_side,
                fill_price=selected.selected_price,
                shares=round(execution.filled_size_usdc / selected.selected_price, 6),
                cost_basis=execution.filled_size_usdc,
                order_id=order_id,
                status=PositionStatus.OPEN,
                paper_trade=True,
                strategy_name=self.strategy_name,
                session_id=self.session_id,
            )
            self.tracker.upsert_position(open_position)
            if self.open_position_alert is not None:
                self.open_position_alert(
                    _paper_open_position_alert_message(
                        position=open_position,
                        market=selected.market,
                    )
                )
            if self.settlement_delay_minutes <= 0:
                settled_trade, closed_position = _paper_trade_from_position(
                    position=open_position,
                    entry_market=selected.market,
                    exit_market=selected.market,
                    exit_price=self.resolver(selected),
                    timestamp=timestamp,
                    score=selected.score,
                    hours_to_close=selected.hours_to_close,
                    strategy_name=self.strategy_name,
                )
                self.tracker.upsert_position(closed_position, notes="paper immediate settlement")
                self.tracker.record_trade(settled_trade)
                resolved_trade = settled_trade
                bankroll_delta = round(bankroll_delta + (settled_trade.pnl or 0.0), 6)
                updated_trades = (*updated_trades, settled_trade)
            else:
                self._open_positions[selected.market.market_id] = _PaperOpenPosition(
                    position=open_position,
                    entry_market=selected.market,
                    opened_at=timestamp,
                    close_after=timestamp + timedelta(minutes=self.settlement_delay_minutes),
                    order_id=order_id,
                )
                bankroll_delta = round(bankroll_delta - execution.filled_size_usdc, 6)

        open_orders = self.tracker.open_order_count(self.strategy_name, session_id=self.session_id)
        open_positions = self.tracker.open_position_count(self.strategy_name, session_id=self.session_id)
        realized_pnl = settled_trade.pnl if settled_trade is not None else 0.0
        updated_state = replace(
            current_state,
            bankroll=round(current_state.bankroll + bankroll_delta, 6),
            daily_pnl=round(current_state.daily_pnl + realized_pnl, 6),
            weekly_pnl=round(current_state.weekly_pnl + realized_pnl, 6),
            total_trades=current_state.total_trades + (1 if settled_trade is not None else 0),
            recent_trades=updated_trades,
            open_orders=open_orders,
            open_positions=open_positions,
            is_paused=False,
            pause_level=None,
            pause_reason=None,
            pause_until=None,
            strategy_name=self.strategy_name,
        )
        self.tracker.record_state(updated_state, session_id=self.session_id)
        self.state = updated_state
        return PaperTradeCycleResult(
            trade=settled_trade,
            kill_signal=None,
            state=updated_state,
        )

    def _settle_due_positions(
        self,
        *,
        timestamp: datetime,
        market_lookup: dict[str, Market],
    ) -> tuple[Trade | None, float]:
        if self.settlement_delay_minutes <= 0 or not self._open_positions:
            return None, 0.0
        due_market_id = next(
            (
                market_id
                for market_id, open_position in self._open_positions.items()
                if timestamp >= open_position.close_after
            ),
            None,
        )
        if due_market_id is None:
            return None, 0.0
        open_position = self._open_positions[due_market_id]
        exit_market = market_lookup.get(due_market_id, open_position.entry_market)
        exit_price = _market_exit_price(exit_market, open_position.position.side)
        settled_trade, closed_position = _paper_trade_from_position(
            position=open_position.position,
            entry_market=open_position.entry_market,
            exit_market=exit_market,
            exit_price=exit_price,
            timestamp=timestamp,
            score=None,
            hours_to_close=None,
            strategy_name=self.strategy_name,
        )
        self.tracker.upsert_position(closed_position, notes=f"paper_close_order_id={open_position.order_id}")
        order = self.tracker.get_order_by_order_id(open_position.order_id)
        requested_size = float(order["requested_size"]) if order is not None else open_position.position.cost_basis
        terminal_status = "FILLED" if open_position.position.cost_basis + 1e-9 >= requested_size else "EXPIRED"
        self.tracker.close_order(
            order_id=open_position.order_id,
            strategy_name=self.strategy_name,
            session_id=self.session_id,
            status=terminal_status,
            last_seen_status=terminal_status,
            last_seen_at=timestamp,
            filled_size=open_position.position.cost_basis,
            exchange_payload={
                "paper_settlement_delay_minutes": self.settlement_delay_minutes,
                "closed_at": timestamp.isoformat(),
                "entry_market_id": open_position.entry_market.market_id,
            },
        )
        del self._open_positions[due_market_id]
        bankroll_delta = closed_position.shares * exit_price
        return settled_trade, bankroll_delta

    async def run(
        self,
        *,
        cycles: int,
        start_at: datetime | None = None,
        step: timedelta = timedelta(minutes=1),
        blocked_market_ids: set[str] | None = None,
    ) -> BotState:
        if cycles <= 0:
            raise ValueError("cycles must be positive")
        current = start_at or datetime.now(UTC)
        for _ in range(cycles):
            result = await self.run_cycle(as_of=current, blocked_market_ids=blocked_market_ids)
            if result.kill_signal is not None:
                break
            current += step
        return self.state


def default_resolver(ranked_market: RankedMarket) -> float:
    return 1.0 if ranked_market.selected_price >= 0.5 else 0.0


def _normalize_market_universe(
    market_universe: Sequence[Market] | Mapping[str, Market],
) -> dict[str, Market]:
    if isinstance(market_universe, Mapping):
        return {str(key): value for key, value in market_universe.items()}
    return {market.market_id: market for market in market_universe}


def _filter_market_universe(
    scanner: object,
    markets: list[Market],
    *,
    as_of: datetime,
    spot_snapshot: SpotSnapshot | dict[str, SpotSnapshot] | None,
    catalyst_snapshot: CatalystSnapshot | None,
) -> list[Market]:
    filter_markets = getattr(scanner, "filter_markets", None)
    if callable(filter_markets):
        return filter_markets(
            markets,
            as_of=as_of,
            spot_snapshot=spot_snapshot,
            catalyst_snapshot=catalyst_snapshot,
        )
    return markets


def _market_exit_price(market: Market, side: OutcomeSide) -> float:
    return market.yes_price if side is OutcomeSide.YES else market.no_price


def _paper_trade_from_position(
    *,
    position: Position,
    entry_market: Market,
    exit_market: Market,
    exit_price: float,
    timestamp: datetime,
    score: float | None,
    hours_to_close: float | None,
    strategy_name: str | None,
) -> tuple[Trade, Position]:
    closed_position = position.settle(resolution_price=exit_price, resolved_at=timestamp)
    trade = Trade(
        timestamp=position.timestamp,
        market_id=position.market_id,
        market_question=position.market_question,
        category=position.category,
        side=position.side,
        entry_price=position.fill_price,
        position_size=position.cost_basis,
        score=score,
        hours_to_close=hours_to_close,
        paper_trade=True,
        order_id=position.order_id,
        strategy_name=strategy_name,
        session_id=position.session_id,
        screened_price=position.fill_price,
        fill_price=position.fill_price,
        resolution_price=exit_price,
    ).settle(resolution_price=exit_price, settled_at=timestamp)
    return trade, closed_position


def _paper_order_id(market_id: str, timestamp: datetime) -> str:
    return f"paper-{market_id}-{int(timestamp.timestamp())}-{uuid4().hex[:8]}"


def _paper_open_position_alert_message(*, position: Position, market: Market) -> str:
    asset = _paper_market_asset(market)
    url = _paper_market_url(market)
    direction = position.side.value
    return (
        "PAPER POSITION OPENED\n"
        f"Strategy: {position.strategy_name or 'unknown'}\n"
        f"Asset: {asset}\n"
        f"Direction: {direction}\n"
        f"Entry price: ${position.fill_price:.3f}\n"
        f"Cost basis: ${position.cost_basis:.2f}\n"
        f"Market: {url}"
    )


def _paper_market_asset(market: Market) -> str:
    text = " ".join(
        field for field in (market.question, market.slug, market.event_slug, market.event_title, market.category) if field
    ).lower()
    if re.search(r"\b(bitcoin|btc)\b", text):
        return "BTC"
    if re.search(r"\b(ethereum|eth)\b", text):
        return "ETH"
    if re.search(r"\b(solana|sol)\b", text):
        return "SOL"
    return market.category.upper() if market.category else "UNKNOWN"


def _paper_market_url(market: Market) -> str:
    slug = market.slug or market.event_slug
    if slug:
        return f"https://polymarket.com/event/{slug}"
    return f"https://polymarket.com/market/{market.market_id}"
