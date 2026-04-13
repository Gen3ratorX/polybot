from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from api.catalyst import CatalystEvent, TradingEconomicsCalendarClient
from bot.catalyst import load_active_catalyst_snapshot
from bot.config import load_runtime_config
from bot.tracker import TradeTracker


@dataclass
class FakeResponse:
    payload: object
    status: int = 200

    async def json(self) -> object:
        return self.payload

    async def text(self) -> str:
        return json.dumps(self.payload)

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeSession:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def get(self, url: str, params: dict[str, object] | None = None) -> FakeResponse:
        params = dict(params or {})
        self.calls.append((url, params))
        return FakeResponse(self.payload)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_trading_economics_calendar_client_parses_events() -> None:
    session = FakeSession(
        [
            {
                "CalendarId": "1001",
                "Date": "2026-04-06T13:30:00",
                "Country": "United States",
                "Category": "Inflation",
                "Event": "CPI",
                "Importance": 3,
                "Source": "BLS",
                "SourceURL": "https://www.bls.gov",
                "URL": "/calendar/cpi",
                "Ticker": "CPIUSA",
                "Symbol": "CPIUSA",
            },
            {
                "CalendarId": "1002",
                "Date": "2026-04-06T14:00:00",
                "Country": "United States",
                "Category": "Labor",
                "Event": "Jobless Claims",
                "Importance": 2,
                "Source": "DOL",
                "SourceURL": "https://www.dol.gov",
                "URL": "/calendar/jobless-claims",
                "Ticker": "IJCUSA",
                "Symbol": "IJCUSA",
            },
        ]
    )
    client = TradingEconomicsCalendarClient(session=session)

    events = await client.fetch_events(
        countries=("united states",),
        start_date=datetime(2026, 4, 6, 12, 0, tzinfo=UTC),
        end_date=datetime(2026, 4, 7, 12, 0, tzinfo=UTC),
    )

    assert [event.calendar_id for event in events] == ["1001", "1002"]
    assert events[0].event == "CPI"
    assert events[0].importance == 3
    assert events[0].country == "United States"
    assert session.calls[0][0].endswith("/calendar/country/united%20states/2026-04-06/2026-04-07")


@pytest.mark.asyncio
async def test_load_active_catalyst_snapshot_records_and_filters_active_events(tmp_path, monkeypatch) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'catalyst.db'}")
    tracker.initialize()
    runtime = load_runtime_config("config.yaml", strategy_section="btc_up_down")
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    active_event = CatalystEvent(
        calendar_id="2001",
        country="United States",
        category="Federal Reserve",
        event="FOMC Press Conference",
        date=reference + timedelta(minutes=20),
        importance=3,
        source="Trading Economics",
        payload={"Event": "FOMC Press Conference"},
    )
    inactive_event = CatalystEvent(
        calendar_id="2002",
        country="United States",
        category="Labor",
        event="Jobless Claims",
        date=reference + timedelta(hours=5),
        importance=2,
        source="Trading Economics",
        payload={"Event": "Jobless Claims"},
    )

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def fetch_events(self, *, countries, start_date, end_date):
            return (active_event, inactive_event)

    monkeypatch.setattr("bot.catalyst.TradingEconomicsCalendarClient", lambda **kwargs: FakeClient())

    snapshot = await load_active_catalyst_snapshot(
        env=None,
        runtime=runtime,
        tracker=tracker,
        as_of=reference,
    )

    assert snapshot is not None
    assert snapshot.provider == "trading_economics"
    assert snapshot.fetch_latency_seconds is not None
    assert [event.calendar_id for event in snapshot.events] == ["2001", "2002"]
    assert [event.calendar_id for event in snapshot.active_events] == ["2001"]
    cached = tracker.list_recent_catalyst_events("trading_economics", limit=10)
    assert [event.calendar_id for event in cached] == ["2002", "2001"]


@pytest.mark.asyncio
async def test_load_active_catalyst_snapshot_fails_closed_on_feed_error(tmp_path, monkeypatch) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'catalyst-fail-closed.db'}")
    tracker.initialize()
    runtime = load_runtime_config("config.yaml", strategy_section="btc_up_down")
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    event = CatalystEvent(
        calendar_id="3001",
        country="United States",
        category="Federal Reserve",
        event="FOMC Press Conference",
        date=reference + timedelta(minutes=20),
        importance=3,
        source="Trading Economics",
        payload={"Event": "FOMC Press Conference"},
    )
    tracker.record_catalyst_events((event,), provider="trading_economics")

    class FailingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def fetch_events(self, *, countries, start_date, end_date):
            raise RuntimeError("feed timeout")

    monkeypatch.setattr("bot.catalyst.TradingEconomicsCalendarClient", lambda **kwargs: FailingClient())

    snapshot = await load_active_catalyst_snapshot(
        env=None,
        runtime=runtime,
        tracker=tracker,
        as_of=reference,
    )

    assert snapshot is None
