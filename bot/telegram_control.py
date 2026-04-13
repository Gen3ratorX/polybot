from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Iterable

from api.telegram import TelegramBotClient, TelegramMessage, TelegramUpdate
from bot.config import EnvironmentConfig
from bot.process_lock import acquire_database_lock
from bot.runtime_state import send_optional_alert
from bot.tracker import TradeTracker


PROFILE_COMMANDS = {"status", "pause", "resume", "run_once", "start", "stop"}
GLOBAL_COMMANDS = {"status", "pause", "resume", "stop_all"}
KNOWN_PROFILES = ("late_market_edge", "btc_up_down")


@dataclass(frozen=True, slots=True)
class TelegramCommandResult:
    handled: bool
    command: str | None
    scope: str | None
    profile_name: str | None
    response: str | None


class TelegramCommandRouter:
    def __init__(self, *, env: EnvironmentConfig, tracker: TradeTracker) -> None:
        self.env = env
        self.tracker = tracker

    async def run(self, *, poll_interval_seconds: float = 5.0) -> None:
        if not self.env.telegram_token or not self.env.telegram_chat_id:
            raise ValueError("Telegram router requires TELEGRAM_TOKEN and TELEGRAM_CHAT_ID")
        async with TelegramBotClient(self.env.telegram_token) as client:
            while True:
                try:
                    await self._poll_once(client)
                except Exception as exc:
                    send_optional_alert(
                        self.env,
                        f"Telegram router error: {exc}",
                        level="WARN",
                    )
                await asyncio.sleep(max(1.0, poll_interval_seconds))

    async def _poll_once(self, client: TelegramBotClient) -> None:
        state = self.tracker.get_telegram_router_state() or {"last_update_id": 0}
        offset = int(state["last_update_id"]) + 1
        updates = await client.get_updates(offset=offset, timeout_seconds=20)
        if not updates:
            return
        max_update_id = max(update.update_id for update in updates)
        for update in updates:
            await self.handle_update(client, update)
        self.tracker.set_telegram_router_state(last_update_id=max_update_id, last_error=None)

    async def handle_update(self, client: TelegramBotClient, update: TelegramUpdate) -> TelegramCommandResult:
        if update.message is None or update.message.text is None:
            return TelegramCommandResult(False, None, None, None, None)
        if self.env.telegram_chat_id and str(update.message.chat_id) != str(self.env.telegram_chat_id):
            return TelegramCommandResult(False, None, None, None, None)
        command, profile_name = parse_command(update.message)
        if command is None:
            return TelegramCommandResult(False, None, None, None, None)
        return await self.route_command(
            client,
            command=command,
            profile_name=profile_name,
            message=update.message,
        )

    async def route_command(
        self,
        client: TelegramBotClient,
        *,
        command: str,
        profile_name: str | None,
        message: TelegramMessage,
    ) -> TelegramCommandResult:
        command = command.lower().strip()
        if command == "stop_all":
            with acquire_database_lock(self.env.database_url):
                control = self.tracker.upsert_control_state(
                    profile_name=None,
                    desired_state="STOPPED",
                    run_once_pending=0,
                    updated_by=_message_author(message),
                    source_chat_id=message.chat_id,
                    source_message_id=message.message_id,
                    last_command="/stop_all",
                    notes="Telegram stop_all",
                )
            response = self._render_global_control_status(control)
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, "GLOBAL", None, response)

        if command in {"pause", "resume"} and profile_name is None:
            desired_state = "PAUSED" if command == "pause" else "RUNNING"
            with acquire_database_lock(self.env.database_url):
                control = self.tracker.upsert_control_state(
                    profile_name=None,
                    desired_state=desired_state,
                    run_once_pending=0,
                    updated_by=_message_author(message),
                    source_chat_id=message.chat_id,
                    source_message_id=message.message_id,
                    last_command=f"/{command}",
                    notes="Telegram global control",
                )
            response = self._render_global_control_status(control)
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, "GLOBAL", None, response)

        if command == "status" and profile_name is None:
            response = self._render_global_status()
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, "GLOBAL", None, response)

        if profile_name is None:
            response = self._usage_message(command)
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, None, None, response)

        if command == "status":
            response = self._render_profile_status(profile_name)
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, "PROFILE", profile_name, response)

        if command in {"pause", "resume", "start", "stop", "run_once"}:
            with acquire_database_lock(self.env.database_url):
                current = self.tracker.resolve_control_state(profile_name)
                if command == "run_once" and current.effective_state == "STOPPED":
                    response = f"{profile_name}: run_once rejected because the profile is stopped."
                    await client.send_message(message.chat_id, response)
                    return TelegramCommandResult(True, command, "PROFILE", profile_name, response)
                if command == "run_once":
                    control = self.tracker.upsert_control_state(
                        profile_name=profile_name,
                        desired_state=current.profile_state.desired_state,
                        run_once_delta=1,
                        updated_by=_message_author(message),
                        source_chat_id=message.chat_id,
                        source_message_id=message.message_id,
                        last_command="/run_once",
                        notes="Telegram run_once",
                    )
                else:
                    desired_state = "RUNNING" if command in {"resume", "start"} else "PAUSED" if command == "pause" else "STOPPED"
                    control = self.tracker.upsert_control_state(
                        profile_name=profile_name,
                        desired_state=desired_state,
                        run_once_pending=0,
                        updated_by=_message_author(message),
                        source_chat_id=message.chat_id,
                        source_message_id=message.message_id,
                        last_command=f"/{command}",
                        notes=f"Telegram {command}",
                    )
            response = self._render_profile_status(profile_name)
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, "PROFILE", profile_name, response)

        response = self._usage_message(command)
        await client.send_message(message.chat_id, response)
        return TelegramCommandResult(True, command, None, profile_name, response)

    def _render_global_status(self) -> str:
        lines = ["Global control status:"]
        global_control = self.tracker.get_control_state(None)
        if global_control is None:
            lines.append("global=RUNNING")
        else:
            lines.append(
                f"global={global_control.desired_state} run_once={global_control.run_once_pending}"
            )
        for profile in _known_profiles(self.tracker):
            lines.append(self._profile_status_line(profile))
        return "\n".join(lines)

    def _render_global_control_status(self, control) -> str:
        return (
            "Global control updated:\n"
            f"global={control.desired_state} run_once={control.run_once_pending}"
        )

    def _render_profile_status(self, profile_name: str, control_override=None) -> str:
        control = control_override or self.tracker.resolve_control_state(profile_name)
        latest_state = self.tracker.get_latest_state(strategy_name=profile_name)
        report = next((item for item in self.tracker.profile_performance_report() if item["strategy_name"] == profile_name), None)
        lines = [
            f"Profile status: {profile_name}",
            f"global={control.global_state.desired_state}",
            f"profile={control.profile_state.desired_state}",
            f"effective={control.effective_state}",
            f"run_once_pending={control.run_once_pending}",
            f"bankroll={0.0 if latest_state is None else latest_state.bankroll:.2f}",
            f"open_orders={0 if latest_state is None else latest_state.open_orders}",
            f"open_positions={0 if latest_state is None else latest_state.open_positions}",
            f"trade_count={0 if latest_state is None else latest_state.total_trades}",
        ]
        if report is not None:
            lines.append(f"win_rate={_format_pct(report['win_rate'])}")
            lines.append(f"pnl=${float(report['total_pnl']):.2f}")
        return "\n".join(lines)

    def _profile_status_line(self, profile_name: str) -> str:
        control = self.tracker.resolve_control_state(profile_name)
        latest_state = self.tracker.get_latest_state(strategy_name=profile_name)
        bankroll = 0.0 if latest_state is None else latest_state.bankroll
        return (
            f"{profile_name}: effective={control.effective_state} "
            f"run_once={control.run_once_pending} "
            f"bankroll={bankroll:.2f}"
        )

    def _usage_message(self, command: str) -> str:
        if command in GLOBAL_COMMANDS:
            return (
                "Unknown or incomplete global command.\n"
                "Use: /status | /pause | /resume | /stop_all"
            )
        return (
            "Unknown or incomplete profile command.\n"
            "Use: /status <profile> | /pause <profile> | /resume <profile> | "
            "/run_once <profile> | /start <profile> | /stop <profile>"
        )


def parse_command(message: TelegramMessage) -> tuple[str | None, str | None]:
    text = message.text or ""
    if not text.startswith("/"):
        return None, None
    parts = text.strip().split()
    command = parts[0].lstrip("/").split("@", 1)[0].lower()
    if command == "stop_all":
        return command, None
    if len(parts) >= 2:
        return command, parts[1].strip()
    return command, None


def _known_profiles(tracker: TradeTracker) -> tuple[str, ...]:
    names = {profile or "unassigned" for profile in (row["strategy_name"] for row in tracker.profile_performance_report())}
    names.update(KNOWN_PROFILES)
    return tuple(sorted(name for name in names if name != "unassigned"))


def _message_author(message: TelegramMessage) -> str | None:
    if message.username:
        return message.username
    if message.from_user_id is not None:
        return str(message.from_user_id)
    return None


def _format_pct(value: object) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1%}"
