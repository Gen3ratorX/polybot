from __future__ import annotations

from dataclasses import dataclass

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bot.tracker import TradeTracker
from models import BotState, Position, Trade


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    bankroll: float
    phase: int
    total_trades: int
    open_positions: int
    is_paused: bool
    pause_reason: str | None
    recent_trades: tuple[Trade, ...]
    active_positions: tuple[Position, ...]
    win_rate_50: float | None
    profile_performance: tuple[dict[str, object], ...] = ()


class DashboardRenderer:
    def snapshot_from_tracker(
        self,
        tracker: TradeTracker,
        *,
        recent_trade_limit: int = 10,
    ) -> DashboardSnapshot:
        state = tracker.get_latest_state(recent_trade_limit=recent_trade_limit)
        if state is None:
            recent_trades = tracker.list_recent_trades(limit=recent_trade_limit)
            active_positions = tracker.list_open_positions(limit=recent_trade_limit)
            resolved = [trade for trade in recent_trades if trade.pnl is not None]
            win_rate = None
            if resolved:
                win_rate = sum(1 for trade in resolved if (trade.pnl or 0.0) > 0) / len(resolved)
            return DashboardSnapshot(
                bankroll=0.0,
                phase=0,
                total_trades=tracker.trade_count(),
                open_positions=len(active_positions),
                is_paused=False,
                pause_reason=None,
                recent_trades=recent_trades,
                active_positions=active_positions,
                win_rate_50=win_rate,
                profile_performance=tuple(tracker.profile_performance_report()),
            )

        return DashboardSnapshot(
            bankroll=state.bankroll,
            phase=state.phase,
            total_trades=state.total_trades,
            open_positions=state.open_positions,
            is_paused=state.is_paused,
            pause_reason=state.pause_reason,
            recent_trades=state.recent_trades[-recent_trade_limit:],
            active_positions=tracker.list_open_positions(limit=recent_trade_limit),
            win_rate_50=state.win_rate(50) if state.recent_trades else None,
            profile_performance=tuple(tracker.profile_performance_report()),
        )

    def render(self, snapshot: DashboardSnapshot):
        summary = Table.grid(padding=(0, 2))
        summary.add_column(style="cyan")
        summary.add_column(style="bold white")
        summary.add_row("Bankroll", f"${snapshot.bankroll:,.2f}")
        summary.add_row("Phase", str(snapshot.phase))
        summary.add_row("Trades", str(snapshot.total_trades))
        summary.add_row("Open Positions", str(snapshot.open_positions))
        summary.add_row(
            "Win Rate (50)",
            "n/a" if snapshot.win_rate_50 is None else f"{snapshot.win_rate_50:.1%}",
        )
        summary.add_row("Paused", "yes" if snapshot.is_paused else "no")
        if snapshot.pause_reason:
            summary.add_row("Pause Reason", snapshot.pause_reason)

        profiles = Table(title="Profile Performance")
        profiles.add_column("Profile", overflow="fold")
        profiles.add_column("Trades")
        profiles.add_column("Open Orders")
        profiles.add_column("Open Pos")
        profiles.add_column("Win Rate")
        profiles.add_column("PnL")
        for profile in snapshot.profile_performance:
            profiles.add_row(
                str(profile["strategy_name"]),
                str(profile["trade_count"]),
                str(profile["open_orders"]),
                str(profile["open_positions"]),
                "n/a" if profile["win_rate"] is None else f"{float(profile['win_rate']):.1%}",
                f"${float(profile['total_pnl']):.2f}",
            )
        if not snapshot.profile_performance:
            profiles.add_row("unassigned", "0", "0", "0", "n/a", "$0.00")

        recent = Table(title="Recent Trades")
        recent.add_column("Market", overflow="fold")
        recent.add_column("Side")
        recent.add_column("Entry")
        recent.add_column("Fill")
        recent.add_column("Slip")
        recent.add_column("Size")
        recent.add_column("Outcome")
        recent.add_column("PnL")
        for trade in snapshot.recent_trades[-10:]:
            recent.add_row(
                trade.market_question,
                trade.side.value,
                f"{trade.entry_price:.3f}",
                "n/a" if trade.fill_price is None else f"{trade.fill_price:.3f}",
                "n/a"
                if trade.fill_slippage is None
                else f"{trade.fill_slippage:+.3f}",
                f"${trade.position_size:.2f}",
                trade.outcome.value,
                "n/a" if trade.pnl is None else f"${trade.pnl:.2f}",
            )
        if not snapshot.recent_trades:
            recent.add_row("No trades logged", "-", "-", "-", "-", "-", "-", "-")

        positions = Table(title="Open Positions")
        positions.add_column("Market", overflow="fold")
        positions.add_column("Side")
        positions.add_column("Fill")
        positions.add_column("Shares")
        positions.add_column("Cost Basis")
        for position in snapshot.active_positions:
            positions.add_row(
                position.market_question,
                position.side.value,
                f"{position.fill_price:.3f}",
                f"{position.shares:.4f}",
                f"${position.cost_basis:.2f}",
            )
        if not snapshot.active_positions:
            positions.add_row("No open positions", "-", "-", "-", "-")

        status_text = Text("PAUSED" if snapshot.is_paused else "RUNNING")
        status_text.stylize("bold red" if snapshot.is_paused else "bold green")

        return Group(
            Panel(summary, title=f"Polybot Status: {status_text}"),
            Panel(profiles),
            Panel(positions),
            Panel(recent),
        )


def build_dashboard(state: BotState):
    snapshot = DashboardSnapshot(
        bankroll=state.bankroll,
        phase=state.phase,
        total_trades=state.total_trades,
        open_positions=state.open_positions,
        is_paused=state.is_paused,
        pause_reason=state.pause_reason,
        recent_trades=state.recent_trades[-10:],
        active_positions=(),
        win_rate_50=state.win_rate(50) if state.recent_trades else None,
        profile_performance=(),
    )
    return DashboardRenderer().render(snapshot)
