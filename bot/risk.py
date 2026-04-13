from __future__ import annotations

from dataclasses import dataclass

from models import BotState


@dataclass(frozen=True, slots=True)
class KillSignal:
    level: str
    reason: str
    pause_minutes: int
    manual_resume_required: bool


class RiskManager:
    DAILY_SOFT_CAP = 0.10
    DAILY_HARD_CAP = 0.15
    WEEKLY_CAP = 0.25
    WIN_RATE_STOP = 0.70
    BALANCE_STOP = 5.0
    FAILURE_LIMIT = 3

    def check_all_kills(self, state: BotState) -> KillSignal | None:
        if state.consecutive_failures >= self.FAILURE_LIMIT:
            return KillSignal(
                "L6",
                "3 consecutive order failures",
                pause_minutes=30,
                manual_resume_required=False,
            )

        if state.bankroll < self.BALANCE_STOP and state.phase <= 1:
            return KillSignal(
                "L5",
                f"Bankroll depleted: ${state.bankroll:.2f}",
                pause_minutes=-1,
                manual_resume_required=True,
            )

        daily_loss_pct = _loss_pct(state.daily_loss, state.bankroll_start_of_day)
        if daily_loss_pct > self.DAILY_HARD_CAP:
            return KillSignal(
                "L2",
                f"Daily loss {daily_loss_pct:.1%}",
                pause_minutes=1440,
                manual_resume_required=False,
            )
        if daily_loss_pct > self.DAILY_SOFT_CAP:
            return KillSignal(
                "L1",
                f"Daily loss {daily_loss_pct:.1%}",
                pause_minutes=360,
                manual_resume_required=False,
            )

        weekly_loss_pct = _loss_pct(state.weekly_loss, state.bankroll_start_of_week)
        if weekly_loss_pct > self.WEEKLY_CAP:
            return KillSignal(
                "L3",
                f"Weekly loss {weekly_loss_pct:.1%}",
                pause_minutes=4320,
                manual_resume_required=True,
            )

        if state.total_trades >= 100:
            recent_100 = state.get_recent_trades(100)
            if len(recent_100) == 100:
                win_rate = sum(1 for trade in recent_100 if (trade.pnl or 0) > 0) / 100
                if win_rate < self.WIN_RATE_STOP:
                    return KillSignal(
                        "L4",
                        f"Win rate {win_rate:.1%}",
                        pause_minutes=-1,
                        manual_resume_required=True,
                    )

        return None


def _loss_pct(loss_amount: float, baseline: float) -> float:
    if baseline <= 0:
        return 0.0
    return loss_amount / baseline
