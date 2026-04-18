from __future__ import annotations

import asyncio
from dataclasses import dataclass

from api.telegram import TelegramBotClient, TelegramMessage, TelegramUpdate
from bot.control_service import ControlMutationResult, ControlService
from bot.config import EnvironmentConfig, load_profile_names
from bot.runtime_state import send_optional_alert
from bot.tracker import TradeTracker


GLOBAL_COMMANDS = {
    "status",
    "pause",
    "resume",
    "stop_all",
    "help",
    "commands",
    "go_live",
    "go_paper",
    "dry_run",
}
GLOBAL_ONLY_COMMANDS = {
    "status",
    "stop_all",
    "help",
    "commands",
    "go_live",
    "go_paper",
    "dry_run",
}


@dataclass(frozen=True, slots=True)
class TelegramCommandResult:
    handled: bool
    command: str | None
    scope: str | None
    profile_name: str | None
    response: str | None


class TelegramCommandRouter:
    def __init__(
        self,
        *,
        env: EnvironmentConfig,
        tracker: TradeTracker,
        configured_profiles: tuple[str, ...] | None = None,
    ) -> None:
        self.env = env
        self.tracker = tracker
        self.configured_profiles = tuple(configured_profiles or load_profile_names())
        self.service = ControlService(
            tracker,
            env.database_url,
            configured_profiles=self.configured_profiles,
        )

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
            result = self.service.apply_global_command(
                command,
                actor=_message_author(message),
                source_chat_id=message.chat_id,
                source_message_id=message.message_id,
                notes="Telegram stop_all",
            )
            response = result.response
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, "GLOBAL", None, response)

        if command in {"help", "commands"} and profile_name is None:
            response = self._help_message()
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, "GLOBAL", None, response)

        if command in {"pause", "resume", "go_live", "go_paper", "dry_run"} and profile_name is None:
            result = self.service.apply_global_command(
                command,
                actor=_message_author(message),
                source_chat_id=message.chat_id,
                source_message_id=message.message_id,
                notes="Telegram global control",
            )
            response = result.response
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, "GLOBAL", None, response)

        if command == "status" and profile_name is None:
            response = self.service.render_global_status()
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, "GLOBAL", None, response)

        if profile_name is None:
            response = self._usage_message(command)
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, None, None, response)

        if command == "status":
            response = self.service.render_profile_status(profile_name)
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, "PROFILE", profile_name, response)

        if command in {"pause", "resume", "start", "stop", "run_once"}:
            result = self.service.apply_profile_command(
                command,
                profile_name,
                actor=_message_author(message),
                source_chat_id=message.chat_id,
                source_message_id=message.message_id,
                notes=f"Telegram {command}",
            )
            response = result.response
            await client.send_message(message.chat_id, response)
            return TelegramCommandResult(True, command, "PROFILE", profile_name, response)

        response = self._usage_message(command)
        await client.send_message(message.chat_id, response)
        return TelegramCommandResult(True, command, None, profile_name, response)

    def _render_global_status(self) -> str:
        return self.service.render_global_status()

    def _render_global_control_status(self, control) -> str:
        return self.service.render_global_status()

    def _render_profile_status(self, profile_name: str, control_override=None) -> str:
        return self.service.render_profile_status(profile_name)

    def _profile_status_line(self, profile_name: str) -> str:
        status = self.service.get_profile_status(profile_name)
        return (
            f"{profile_name}: effective={status['effective_state']} "
            f"run_once={status['run_once_pending']} "
            f"bankroll={float(status['bankroll']):.2f}"
        )

    def _usage_message(self, command: str) -> str:
        if command in GLOBAL_COMMANDS:
            return (
                "Unknown or incomplete global command.\n"
                "Use: /status | /pause | /resume | /stop_all | /go_live | /go_paper | /dry_run | /help"
            )
        return (
            "Unknown or incomplete profile command.\n"
            "Use: /status <profile> | /pause <profile> | /resume <profile> | "
            "/run_once <profile> | /start <profile> | /stop <profile>"
        )

    def _help_message(self) -> str:
        profiles = self.service.list_profiles()
        lines = [
            "Polybot Telegram commands:",
            "",
            "Global:",
            "/status - show overall bot state and all profiles",
            "/pause - pause all profiles",
            "/resume - resume all profiles",
            "/stop_all - stop everything cleanly",
            "/go_live - enable live submit mode",
            "/go_paper - disable live submit mode",
            "/dry_run - alias for /go_paper",
            "",
            "Per-profile:",
        ]
        for profile in profiles:
            lines.extend(
                [
                    f"/status {profile}",
                    f"/pause {profile}",
                    f"/resume {profile}",
                    f"/run_once {profile}",
                    f"/start {profile}",
                    f"/stop {profile}",
                    "",
                ]
            )
        lines.append("Use a profile name to target only that strategy.")
        return "\n".join(lines).rstrip()


def parse_command(message: TelegramMessage) -> tuple[str | None, str | None]:
    text = message.text or ""
    if not text.startswith("/"):
        return None, None
    parts = text.strip().split()
    command = parts[0].lstrip("/").split("@", 1)[0].lower()
    if command in GLOBAL_ONLY_COMMANDS:
        return command, None
    if len(parts) >= 2:
        return command, parts[1].strip()
    return command, None


def _message_author(message: TelegramMessage) -> str | None:
    if message.username:
        return message.username
    if message.from_user_id is not None:
        return str(message.from_user_id)
    return None
