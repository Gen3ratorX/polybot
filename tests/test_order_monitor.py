from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.executor import LiveOrderStatus
from bot.order_monitor import monitor_order_status, wait_for_order_status_via_websocket


class FakeExecutor:
    def __init__(self, status: LiveOrderStatus) -> None:
        self.status = status
        self.calls = 0

    def get_order_status(self, order_id: str) -> LiveOrderStatus:
        self.calls += 1
        return self.status


@pytest.mark.asyncio
async def test_wait_for_order_status_via_websocket_extracts_nested_order_payload() -> None:
    messages = [
        "PONG",
        {
            "event_type": "order",
            "data": {
                "order": {
                    "id": "oid",
                    "status": "MATCHED",
                    "createdAtMs": 1712400000000,
                    "assetId": "asset-1",
                    "originalSize": "5.66",
                    "sizeMatched": "5.66",
                    "price": "0.53",
                }
            },
        }
    ]

    class FakeWebSocket:
        def __init__(self, raw_messages):
            import json

            self.raw_messages = [json.dumps(item) for item in raw_messages]

        async def send(self, payload: str) -> None:
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.raw_messages:
                raise StopAsyncIteration
            return self.raw_messages.pop(0)

    class FakeConnect:
        def __init__(self, ws):
            self.ws = ws

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, *_):
            return False

    env = SimpleNamespace(
        poly_api_key="k",
        poly_api_secret="s",
        poly_api_passphrase="p",
    )
    result = await wait_for_order_status_via_websocket(
        env=env,
        order_id="oid",
        timeout_seconds=1.0,
        heartbeat_seconds=999,
        connect_factory=lambda uri: FakeConnect(FakeWebSocket(messages)),
    )

    assert result is not None
    assert result.order_id == "oid"
    assert result.is_terminal is True
    assert result.matched_size == 5.66


def test_monitor_order_status_falls_back_to_polling(monkeypatch) -> None:
    status = LiveOrderStatus(
        order_id="oid",
        status="CANCELED",
        created_at=None,
        market_condition_id="cond",
        token_id="asset",
        side="BUY",
        price=0.53,
        original_size=5.66,
        matched_size=0.0,
    )
    executor = FakeExecutor(status)
    env = SimpleNamespace(
        poly_api_key="k",
        poly_api_secret="s",
        poly_api_passphrase="p",
    )
    async def no_status(**kwargs):
        return None

    monkeypatch.setattr("bot.order_monitor.wait_for_order_status_via_websocket", no_status)

    result = monitor_order_status(
        env=env,
        executor=executor,
        order_id="oid",
        market_id="cond",
        timeout_seconds=0.01,
        poll_interval=0.01,
        heartbeat_seconds=999,
    )

    assert result.status == "CANCELED"
    assert executor.calls >= 1
