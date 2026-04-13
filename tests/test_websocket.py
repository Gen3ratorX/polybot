from __future__ import annotations

import json

import pytest

from api.websocket import MarketWebSocketClient, UserAuth, UserWebSocketClient


class FakeWebSocket:
    def __init__(self, messages: list[dict], on_send=None) -> None:
        self.messages = [json.dumps(message) for message in messages]
        self.sent: list[dict] = []
        self._on_send = on_send

    async def send(self, payload: str) -> None:
        parsed = json.loads(payload)
        self.sent.append(parsed)
        if self._on_send is not None:
            self._on_send(parsed)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


class FakeConnect:
    def __init__(self, ws: FakeWebSocket) -> None:
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *_):
        return False


@pytest.mark.asyncio
async def test_market_websocket_subscribes_and_receives_message() -> None:
    received = []
    ws = FakeWebSocket([{"event_type": "book", "asset_id": "1"}])
    client = MarketWebSocketClient(
        connect_factory=lambda uri: FakeConnect(ws),
        heartbeat_seconds=999,
    )

    async def handler(message):
        received.append(message)

    await client.subscribe(["1"], handler, max_messages=1)

    assert ws.sent[0]["type"] == "market"
    assert ws.sent[0]["assets_ids"] == ["1"]
    assert received[0]["event_type"] == "book"


@pytest.mark.asyncio
async def test_user_websocket_authenticates_and_receives_message() -> None:
    received = []
    ws = FakeWebSocket([{"event_type": "order", "status": "LIVE"}])
    client = UserWebSocketClient(
        auth=UserAuth(api_key="k", secret="s", passphrase="p"),
        connect_factory=lambda uri: FakeConnect(ws),
        heartbeat_seconds=999,
    )

    async def handler(message):
        received.append(message)

    await client.subscribe(handler, markets=["0xmarket"], max_messages=1)

    assert ws.sent[0]["type"] == "user"
    assert ws.sent[0]["auth"]["apiKey"] == "k"
    assert ws.sent[1]["operation"] == "subscribe"
    assert received[0]["event_type"] == "order"
