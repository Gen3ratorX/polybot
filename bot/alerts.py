from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import aiohttp


@dataclass(frozen=True, slots=True)
class AlertDelivery:
    ok: bool
    status: int
    response: dict[str, Any] | None


class TelegramAlerter:
    EMOJIS = {
        "INFO": "ℹ️",
        "WARN": "⚠️",
        "KILL": "🛑",
        "WIN": "✅",
        "LOSS": "❌",
    }

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.token = token
        self.chat_id = chat_id
        self.session_factory = session_factory or aiohttp.ClientSession

    async def send_alert(self, message: str, level: str = "INFO") -> AlertDelivery:
        text = f"{self.EMOJIS.get(level, '')} POLYBOT ALERT\n\n{message}".strip()
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
        }
        async with self.session_factory() as session:
            async with session.post(url, json=payload) as response:
                body = await response.json()
                return AlertDelivery(
                    ok=response.status < 400 and bool(body.get("ok", True)),
                    status=response.status,
                    response=body,
                )
