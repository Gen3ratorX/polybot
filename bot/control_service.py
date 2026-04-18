from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from bot.process_lock import acquire_database_lock
from bot.tracker import TradeTracker


KNOWN_PROFILES = ("late_market_edge", "btc_up_down")


@dataclass(frozen=True, slots=True)
class ControlMutationResult:
    handled: bool
    command: str
    scope: str
    profile_name: str | None
    response: str


class ControlService:
    def __init__(
        self,
        tracker: TradeTracker,
        database_url: str,
        *,
        configured_profiles: tuple[str, ...] | None = None,
    ) -> None:
        self.tracker = tracker
        self.database_url = database_url
        self.configured_profiles = tuple(name for name in (configured_profiles or ()) if str(name).strip())

    def list_profiles(self) -> tuple[str, ...]:
        observed = {
            row["strategy_name"]
            for row in self.tracker.profile_performance_report()
            if row["strategy_name"] not in (None, "unassigned")
        }
        observed.update(KNOWN_PROFILES)
        observed.update(self.configured_profiles)
        return tuple(sorted(observed))

    def get_global_status(self) -> dict[str, object]:
        control = self.tracker.get_control_state(None)
        if control is None:
            return {
                "desired_state": "RUNNING",
                "run_once_pending": 0,
            }
        return {
            "desired_state": control.desired_state,
            "run_once_pending": control.run_once_pending,
        }

    def get_profile_status(self, profile_name: str) -> dict[str, object]:
        control = self.tracker.resolve_control_state(profile_name)
        latest_state = self.tracker.get_latest_state(strategy_name=profile_name)
        performance = next(
            (item for item in self.tracker.profile_performance_report() if item["strategy_name"] == profile_name),
            {
                "trade_count": 0,
                "wins": 0,
                "losses": 0,
                "unresolved": 0,
                "open_orders": 0,
                "open_positions": 0,
                "win_rate": None,
                "total_pnl": 0.0,
            },
        )
        return {
            "profile_name": profile_name,
            "global_state": control.global_state.desired_state,
            "profile_state": control.profile_state.desired_state,
            "effective_state": control.effective_state,
            "run_once_pending": control.run_once_pending,
            "bankroll": 0.0 if latest_state is None else latest_state.bankroll,
            "open_orders": 0 if latest_state is None else latest_state.open_orders,
            "open_positions": 0 if latest_state is None else latest_state.open_positions,
            "trade_count": 0 if latest_state is None else latest_state.total_trades,
            "win_rate": performance["win_rate"],
            "total_pnl": performance["total_pnl"],
            "latest_state": latest_state,
        }

    def render_global_status(self) -> str:
        lines = ["Global control status:"]
        global_status = self.get_global_status()
        lines.append(
            f"global={global_status['desired_state']} run_once={global_status['run_once_pending']}"
        )
        for profile in self.list_profiles():
            profile_status = self.get_profile_status(profile)
            lines.append(
                f"{profile}: effective={profile_status['effective_state']} "
                f"run_once={profile_status['run_once_pending']} "
                f"bankroll={float(profile_status['bankroll']):.2f}"
            )
        return "\n".join(lines)

    def render_global_control_update(self) -> str:
        return self.render_global_status()

    def render_profile_status(self, profile_name: str) -> str:
        status = self.get_profile_status(profile_name)
        lines = [
            f"Profile status: {profile_name}",
            f"global={status['global_state']}",
            f"profile={status['profile_state']}",
            f"effective={status['effective_state']}",
            f"run_once_pending={status['run_once_pending']}",
            f"bankroll={float(status['bankroll']):.2f}",
            f"open_orders={status['open_orders']}",
            f"open_positions={status['open_positions']}",
            f"trade_count={status['trade_count']}",
            "win_rate="
            + ("n/a" if status["win_rate"] is None else f"{float(status['win_rate']):.1%}"),
            f"pnl=${float(status['total_pnl']):.2f}",
        ]
        return "\n".join(lines)

    def apply_global_command(
        self,
        command: str,
        *,
        actor: str | None = None,
        source_chat_id: str | None = None,
        source_message_id: int | None = None,
        notes: str | None = None,
    ) -> ControlMutationResult:
        normalized = command.strip().lower()
        if normalized == "status":
            return ControlMutationResult(True, normalized, "GLOBAL", None, self.render_global_status())
        if normalized == "stop_all":
            with acquire_database_lock(self.database_url):
                self.tracker.upsert_control_state(
                    profile_name=None,
                    desired_state="STOPPED",
                    run_once_pending=0,
                    updated_by=actor,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    last_command="/stop_all",
                    notes=notes or "Global stop",
                )
                for profile in self.list_profiles():
                    self.tracker.upsert_control_state(
                        profile_name=profile,
                        desired_state="STOPPED",
                        run_once_pending=0,
                        updated_by=actor,
                        source_chat_id=source_chat_id,
                        source_message_id=source_message_id,
                        last_command="/stop_all",
                        notes=notes or "Global stop",
                    )
            return ControlMutationResult(True, normalized, "GLOBAL", None, self.render_global_status())
        if normalized in {"pause", "resume"}:
            desired_state = "PAUSED" if normalized == "pause" else "RUNNING"
            with acquire_database_lock(self.database_url):
                self.tracker.upsert_control_state(
                    profile_name=None,
                    desired_state=desired_state,
                    run_once_pending=0,
                    updated_by=actor,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    last_command=f"/{normalized}",
                    notes=notes or f"Global {normalized}",
                )
            return ControlMutationResult(True, normalized, "GLOBAL", None, self.render_global_status())
        raise ValueError(f"Unsupported global command: {command}")

    def apply_profile_command(
        self,
        command: str,
        profile_name: str,
        *,
        actor: str | None = None,
        source_chat_id: str | None = None,
        source_message_id: int | None = None,
        notes: str | None = None,
    ) -> ControlMutationResult:
        normalized = command.strip().lower()
        if normalized == "status":
            return ControlMutationResult(True, normalized, "PROFILE", profile_name, self.render_profile_status(profile_name))
        with acquire_database_lock(self.database_url):
            current = self.tracker.resolve_control_state(profile_name)
            if normalized == "run_once":
                if current.effective_state == "STOPPED":
                    response = f"{profile_name}: run_once rejected because the profile is stopped."
                    return ControlMutationResult(True, normalized, "PROFILE", profile_name, response)
                self.tracker.upsert_control_state(
                    profile_name=profile_name,
                    desired_state=current.profile_state.desired_state,
                    run_once_delta=1,
                    updated_by=actor,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    last_command="/run_once",
                    notes=notes or "Run once queued",
                )
            elif normalized in {"pause", "resume", "start", "stop"}:
                desired_state = "RUNNING" if normalized in {"resume", "start"} else "PAUSED" if normalized == "pause" else "STOPPED"
                self.tracker.upsert_control_state(
                    profile_name=profile_name,
                    desired_state=desired_state,
                    run_once_pending=0,
                    updated_by=actor,
                    source_chat_id=source_chat_id,
                    source_message_id=source_message_id,
                    last_command=f"/{normalized}",
                    notes=notes or f"Profile {normalized}",
                )
            else:
                raise ValueError(f"Unsupported profile command: {command}")
        return ControlMutationResult(True, normalized, "PROFILE", profile_name, self.render_profile_status(profile_name))
