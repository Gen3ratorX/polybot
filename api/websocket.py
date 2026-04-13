from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import websockets

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class UserAuth:
    api_key: str
    secret: str
    passphrase: str


class BaseWebSocketClient:
    def __init__(
        self,
        uri: str,
        *,
        connect_factory: Callable[..., Any] | None = None,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        self.uri = uri
        self.connect_factory = connect_factory or websockets.connect
        self.heartbeat_seconds = heartbeat_seconds

    async def _run(
        self,
        subscription_message: dict[str, Any],
        callback: MessageHandler,
        *,
        max_messages: int | None = None,
    ) -> None:
        async with self.connect_factory(self.uri) as ws:
            await ws.send(json.dumps(subscription_message))
            heartbeat_task = asyncio.create_task(self._heartbeat(ws))
            try:
                count = 0
                async for raw_message in ws:
                    payload = _parse_message(raw_message)
                    if payload is None:
                        continue
                    await callback(payload)
                    count += 1
                    if max_messages is not None and count >= max_messages:
                        break
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

    async def _heartbeat(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            await ws.send(json.dumps({}))


class MarketWebSocketClient(BaseWebSocketClient):
    def __init__(
        self,
        uri: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        *,
        connect_factory: Callable[..., Any] | None = None,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            uri,
            connect_factory=connect_factory,
            heartbeat_seconds=heartbeat_seconds,
        )

    async def subscribe(
        self,
        asset_ids: list[str],
        callback: MessageHandler,
        *,
        custom_feature_enabled: bool = True,
        max_messages: int | None = None,
    ) -> None:
        await self._run(
            {
                "assets_ids": asset_ids,
                "type": "market",
                "custom_feature_enabled": custom_feature_enabled,
            },
            callback,
            max_messages=max_messages,
        )


class UserWebSocketClient(BaseWebSocketClient):
    def __init__(
        self,
        auth: UserAuth,
        uri: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user",
        *,
        connect_factory: Callable[..., Any] | None = None,
        heartbeat_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            uri,
            connect_factory=connect_factory,
            heartbeat_seconds=heartbeat_seconds,
        )
        self.auth = auth

    async def subscribe(
        self,
        callback: MessageHandler,
        *,
        markets: list[str] | None = None,
        max_messages: int | None = None,
    ) -> None:
        async with self.connect_factory(self.uri) as ws:
            await ws.send(
                json.dumps(
                    {
                        "auth": {
                            "apiKey": self.auth.api_key,
                            "secret": self.auth.secret,
                            "passphrase": self.auth.passphrase,
                        },
                        "type": "user",
                    }
                )
            )
            if markets:
                await ws.send(json.dumps({"operation": "subscribe", "markets": markets}))

            heartbeat_task = asyncio.create_task(self._heartbeat(ws))
            try:
                count = 0
                async for raw_message in ws:
                    payload = _parse_message(raw_message)
                    if payload is None:
                        continue
                    await callback(payload)
                    count += 1
                    if max_messages is not None and count >= max_messages:
                        break
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task


import contextlib  # keep import local to avoid unused-order churn


def _parse_message(raw_message: Any) -> dict[str, Any] | list[Any] | None:
    if isinstance(raw_message, (dict, list)):
        return raw_message
    if not isinstance(raw_message, str):
        return None
    text = raw_message.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, (dict, list)):
        return payload
    return None
