from __future__ import annotations

import asyncio
from typing import Any, Iterable

from api.websocket import UserAuth, UserWebSocketClient
from bot.config import EnvironmentConfig
from bot.executor import LiveOrderStatus, OrderExecutor


def build_user_auth(env: EnvironmentConfig) -> UserAuth | None:
    if not (env.poly_api_key and env.poly_api_secret and env.poly_api_passphrase):
        return None
    return UserAuth(
        api_key=env.poly_api_key,
        secret=env.poly_api_secret,
        passphrase=env.poly_api_passphrase,
    )


async def wait_for_order_status_via_websocket(
    *,
    env: EnvironmentConfig,
    order_id: str,
    timeout_seconds: float,
    heartbeat_seconds: float,
    markets: list[str] | None = None,
    connect_factory=None,
) -> LiveOrderStatus | None:
    auth = build_user_auth(env)
    if auth is None:
        return None

    event = asyncio.Event()
    latest: LiveOrderStatus | None = None

    async def handler(message: dict[str, Any]) -> None:
        nonlocal latest
        for candidate in _iter_order_payloads(message):
            try:
                parsed = OrderExecutor.parse_order_status(candidate)
            except ValueError:
                continue
            if parsed.order_id != order_id:
                continue
            latest = parsed
            if parsed.is_terminal:
                event.set()
                return
        if _message_mentions_order(message, order_id):
            event.set()

    client = UserWebSocketClient(
        auth=auth,
        heartbeat_seconds=heartbeat_seconds,
        connect_factory=connect_factory,
    )
    subscription = asyncio.create_task(client.subscribe(handler, markets=markets))
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return latest
    finally:
        subscription.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await subscription
    return latest


def monitor_order_status(
    *,
    env: EnvironmentConfig,
    executor: OrderExecutor,
    order_id: str,
    market_id: str | None,
    timeout_seconds: float,
    poll_interval: float,
    heartbeat_seconds: float,
) -> LiveOrderStatus:
    websocket_status = asyncio.run(
        wait_for_order_status_via_websocket(
            env=env,
            order_id=order_id,
            timeout_seconds=timeout_seconds,
            heartbeat_seconds=heartbeat_seconds,
            markets=[market_id] if market_id else None,
        )
    )
    if websocket_status is not None:
        return websocket_status
    deadline = time.time() + max(0, timeout_seconds)
    latest = executor.get_order_status(order_id)
    while time.time() < deadline and not latest.is_terminal:
        time.sleep(max(0.1, poll_interval))
        latest = executor.get_order_status(order_id)
    return latest


def _iter_order_payloads(payload: Any) -> Iterable[dict[str, object]]:
    if isinstance(payload, dict):
        if _looks_like_order_payload(payload):
            yield payload
        for key in ("order", "data", "payload", "orders", "matches"):
            value = payload.get(key)
            if isinstance(value, dict):
                yield from _iter_order_payloads(value)
            elif isinstance(value, list):
                for item in value:
                    yield from _iter_order_payloads(item)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_order_payloads(item)


def _looks_like_order_payload(payload: dict[str, object]) -> bool:
    keys = set(payload.keys())
    has_id = bool({"id", "orderID", "orderId"} & keys)
    has_status = "status" in keys
    has_size = bool({"size_matched", "sizeMatched", "original_size", "originalSize"} & keys)
    return has_id and (has_status or has_size)


def _message_mentions_order(payload: Any, order_id: str) -> bool:
    if isinstance(payload, dict):
        for value in payload.values():
            if value == order_id:
                return True
            if isinstance(value, (dict, list)) and _message_mentions_order(value, order_id):
                return True
    elif isinstance(payload, list):
        return any(_message_mentions_order(item, order_id) for item in payload)
    return False


import contextlib  # keep import local to avoid unused-order churn
import time  # keep import local to avoid unused-order churn
