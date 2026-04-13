from __future__ import annotations

import pytest

from bot.alerts import TelegramAlerter


class FakeResponse:
    def __init__(self, payload, status=200) -> None:
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self) -> None:
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def post(self, url, json):
        self.requests.append((url, json))
        return FakeResponse({"ok": True, "result": {"message_id": 1}}, status=200)


@pytest.mark.asyncio
async def test_telegram_alerter_posts_message() -> None:
    session = FakeSession()
    alerter = TelegramAlerter(
        token="token",
        chat_id="chat",
        session_factory=lambda: session,
    )

    result = await alerter.send_alert("hello", level="WARN")

    assert result.ok is True
    assert session.requests[0][0].endswith("/sendMessage")
    assert "POLYBOT ALERT" in session.requests[0][1]["text"]
    assert "hello" in session.requests[0][1]["text"]
