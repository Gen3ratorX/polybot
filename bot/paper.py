from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from uuid import uuid4
from typing import Protocol

from bot.ranker import EdgeRanker, RankedMarket
from bot.risk import KillSignal, RiskManager
from api.catalyst import CatalystSnapshot
from api.spot import SpotSnapshot
from bot.tracker import TradeTracker
from models import BotState, Trade


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
    ) -> None:
        if initial_bankroll <= 0:
            raise ValueError("initial_bankroll must be positive")
        if trade_size_usd <= 0:
            raise ValueError("trade_size_usd must be positive")

        start = datetime.now(UTC)
        self.scanner = scanner
        self.ranker = ranker
        self.tracker = tracker
        self.risk_manager = risk_manager
        self.trade_size_usd = trade_size_usd
        self.strategy_name = strategy_name
        self.resolver = resolver or default_resolver
        self.execution_simulator = execution_simulator or PaperExecutionSimulator()
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
            self.tracker.record_state(paused_state)
            self.state = paused_state
            return PaperTradeCycleResult(trade=None, kill_signal=kill_signal, state=paused_state)

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
        ranked = self.ranker.rank_markets(candidates, as_of=timestamp)
        if blocked_market_ids:
            ranked = [item for item in ranked if item.market.market_id not in blocked_market_ids]
        if not ranked:
            self.state = current_state
            return PaperTradeCycleResult(trade=None, kill_signal=None, state=current_state)

        selected = ranked[0]
        position_size = min(self.trade_size_usd, current_state.bankroll)
        execution = self.execution_simulator.simulate(requested_size_usdc=position_size)
        order_id = _paper_order_id(selected.market.market_id, timestamp)
        self.tracker.record_order(
            order_id=order_id,
            market_id=selected.market.market_id,
            strategy_name=self.strategy_name,
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

        resolved_trade = None
        bankroll_delta = 0.0
        updated_trades = current_state.recent_trades
        if execution.filled_size_usdc > 0:
            trade = Trade(
                timestamp=timestamp,
                market_id=selected.market.market_id,
                market_question=selected.market.question,
                category=selected.market.category,
                side=selected.selected_side,
                entry_price=selected.selected_price,
                position_size=execution.filled_size_usdc,
                score=selected.score,
                hours_to_close=selected.hours_to_close,
                paper_trade=True,
                order_id=order_id,
                strategy_name=self.strategy_name,
                screened_price=selected.selected_price,
                fill_price=selected.selected_price,
            )
            resolved_trade = trade.settle(
                resolution_price=self.resolver(selected),
                settled_at=timestamp,
            )
            bankroll_delta = resolved_trade.pnl or 0.0
            updated_trades = (*current_state.recent_trades, resolved_trade)
            self.tracker.record_trade(resolved_trade)

        open_orders = self.tracker.open_order_count(self.strategy_name)
        updated_state = replace(
            current_state,
            bankroll=round(current_state.bankroll + bankroll_delta, 6),
            daily_pnl=round(current_state.daily_pnl + bankroll_delta, 6),
            weekly_pnl=round(current_state.weekly_pnl + bankroll_delta, 6),
            total_trades=current_state.total_trades + (1 if resolved_trade is not None else 0),
            recent_trades=updated_trades,
            open_orders=open_orders,
            is_paused=False,
            pause_level=None,
            pause_reason=None,
            pause_until=None,
            strategy_name=self.strategy_name,
        )
        self.tracker.record_state(updated_state)
        self.state = updated_state
        return PaperTradeCycleResult(
            trade=resolved_trade,
            kill_signal=None,
            state=updated_state,
        )

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


def _paper_order_id(market_id: str, timestamp: datetime) -> str:
    return f"paper-{market_id}-{int(timestamp.timestamp())}-{uuid4().hex[:8]}"
