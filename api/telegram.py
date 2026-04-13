from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


class TelegramAPIError(RuntimeError):
    """Raised when the Telegram API returns an invalid response."""


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    message_id: int
    chat_id: str
    text: str | None
    from_user_id: int | None
    username: str | None
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    update_id: int
    message: TelegramMessage | None
    raw: dict[str, object]


class TelegramBotClient:
    def __init__(
        self,
        token: str,
        *,
        session: aiohttp.ClientSession | Any | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def __aenter__(self) -> "TelegramBotClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

    async def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_seconds: int = 20,
        allowed_updates: tuple[str, ...] = ("message",),
    ) -> list[TelegramUpdate]:
        payload = await self._request(
            "getUpdates",
            params={
                "offset": offset,
                "timeout": timeout_seconds,
                "allowed_updates": list(allowed_updates),
            },
        )
        if not isinstance(payload, dict):
            raise TelegramAPIError("Expected getUpdates response to be an object")
        if not payload.get("ok", False):
            raise TelegramAPIError(f"Telegram getUpdates failed: {payload}")
        result = payload.get("result")
        if not isinstance(result, list):
            raise TelegramAPIError("Expected Telegram getUpdates result to be a list")
        return [update for item in result if (update := _parse_update(item)) is not None]

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, object]:
        payload = await self._request(
            "sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "disable_web_page_preview": disable_web_page_preview,
            },
        )
        if not isinstance(payload, dict):
            raise TelegramAPIError("Expected sendMessage response to be an object")
        if not payload.get("ok", False):
            raise TelegramAPIError(f"Telegram sendMessage failed: {payload}")
        return payload

    async def _request(
        self,
        method: str,
        *,
        params: dict[str, object | None] | None = None,
        json: dict[str, object | None] | None = None,
    ) -> object:
        session = await self._ensure_session()
        cleaned_params = {key: value for key, value in (params or {}).items() if value is not None}
        cleaned_json = {key: value for key, value in (json or {}).items() if value is not None}
        async with session.post(
            f"{self.base_url}/{method}",
            params=cleaned_params if cleaned_params else None,
            json=cleaned_json if cleaned_json else None,
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise TelegramAPIError(f"Telegram API request failed ({response.status}): {body[:200]}")
            return await response.json()

    async def _ensure_session(self) -> aiohttp.ClientSession | Any:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session


def _parse_update(payload: object) -> TelegramUpdate | None:
    if not isinstance(payload, dict):
        return None
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        return None
    message_payload = payload.get("message")
    message = _parse_message(message_payload) if isinstance(message_payload, dict) else None
    return TelegramUpdate(update_id=update_id, message=message, raw=payload)


def _parse_message(payload: dict[str, object]) -> TelegramMessage | None:
    message_id = payload.get("message_id")
    chat_payload = payload.get("chat")
    text = payload.get("text")
    from_payload = payload.get("from")
    if not isinstance(message_id, int) or not isinstance(chat_payload, dict):
        return None
    chat_id = chat_payload.get("id")
    if chat_id is None:
        return None
    from_user_id = None
    username = None
    if isinstance(from_payload, dict):
        from_id = from_payload.get("id")
        if isinstance(from_id, int):
            from_user_id = from_id
        from_username = from_payload.get("username")
        if isinstance(from_username, str):
            username = from_username
    return TelegramMessage(
        message_id=message_id,
        chat_id=str(chat_id),
        text=text if isinstance(text, str) else None,
        from_user_id=from_user_id,
        username=username,
        raw=payload,
    )
