from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from api.spot import SpotFeedClient
from bot.tracker import TradeTracker


@dataclass
class FakeResponse:
    payload: dict[str, object]
    status: int = 200

    async def json(self) -> dict[str, object]:
        return self.payload

    async def text(self) -> str:
        return json.dumps(self.payload)

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def get(self, url: str, params: dict[str, object] | None = None) -> FakeResponse:
        params = dict(params or {})
        self.calls.append((url, params))
        if url.endswith("/0/public/Ticker"):
            return FakeResponse(
                {
                    "error": [],
                    "result": {
                        "XBTUSD": {
                            "c": ["70500.12", "1.0"],
                        }
                    },
                }
            )
        if url.endswith("/0/public/OHLC"):
            interval = params.get("interval")
            if interval == 15:
                rows = [
                    [1712400000, "69400.0", "70600.0", "69200.0", "70000.0", "69800.0", "12.0", 12],
                    [1712400900, "70000.0", "70750.0", "69900.0", "70500.0", "70400.0", "8.0", 10],
                ]
            else:
                rows = [
                    [1712396400, "67800.0", "68200.0", "67700.0", "68000.0", "67900.0", "60.0", 60],
                    [1712400000, "68000.0", "70750.0", "67950.0", "70500.0", "70200.0", "42.0", 55],
                ]
            return FakeResponse({"error": [], "result": {"XBTUSD": rows, "last": 1712400900}})
        return FakeResponse({"error": ["unknown endpoint"], "result": {}})

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_spot_feed_client_parses_kraken_snapshot() -> None:
    session = FakeSession()
    client = SpotFeedClient(session=session)

    snapshot = await client.fetch_snapshot("XBTUSD")

    assert snapshot.symbol == "XBTUSD"
    assert snapshot.pair == "XBTUSD"
    assert snapshot.spot_price == 70500.12
    assert snapshot.return_15m_pct == 0.007143
    assert snapshot.return_1h_pct == 0.036765
    assert snapshot.source == "kraken"
    assert len(session.calls) == 3
    assert session.closed is False


def test_tracker_round_trips_spot_snapshots(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'spot.db'}")
    tracker.initialize()
    observed_at = datetime.now(UTC) - timedelta(seconds=10)
    from api.spot import SpotSnapshot

    snapshot = SpotSnapshot(
        symbol="XBTUSD",
        pair="XBTUSD",
        spot_price=70500.12,
        return_15m_pct=0.007143,
        return_1h_pct=0.036765,
        fetch_latency_seconds=0.123,
        observed_at=observed_at,
        age_seconds=10.0,
        source="kraken",
        payload={"ticker": {"c": ["70500.12"]}},
    )

    tracker.record_spot_tick(snapshot)

    latest = tracker.get_latest_spot_snapshot("XBTUSD")
    assert latest is not None
    assert latest.symbol == "XBTUSD"
    assert latest.pair == "XBTUSD"
    assert latest.spot_price == 70500.12
    assert latest.return_15m_pct == 0.007143
    assert latest.return_1h_pct == 0.036765
    assert latest.fetch_latency_seconds == 0.123
    assert latest.age_seconds >= 0.0
    assert tracker.list_recent_spot_snapshots("XBTUSD", limit=1)[0].spot_price == 70500.12
