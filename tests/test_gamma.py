from __future__ import annotations

from collections import deque

import pytest

from api.gamma import GammaAPIError, GammaClient
from models import Market, OutcomeSide


class FakeResponse:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def json(self) -> object:
        return self.payload

    async def text(self) -> str:
        return str(self.payload)


class FakeSession:
    def __init__(self, payloads: list[object], *, statuses: list[int] | None = None) -> None:
        self.payloads = deque(payloads)
        self.statuses = deque(statuses or [200] * len(payloads))
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def get(self, url: str, params: dict[str, object] | None = None) -> FakeResponse:
        self.calls.append((url, params or {}))
        return FakeResponse(self.payloads.popleft(), status=self.statuses.popleft())

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_fetch_markets_normalizes_market_payload() -> None:
    session = FakeSession(
        [
            [
                {
                    "id": "123",
                    "question": "Will it rain tomorrow?",
                    "conditionId": "0xabc",
                    "endDate": "2026-04-06T18:00:00Z",
                    "outcomePrices": '["0.90","0.10"]',
                    "clobTokenIds": '["yes","no"]',
                    "volumeNum": None,
                    "volume": "550.0",
                    "volume24hr": 45.2,
                    "liquidity": "1200.0",
                    "oneHourPriceChange": 0.01,
                    "active": True,
                    "closed": False,
                    "archived": False,
                }
            ]
        ]
    )
    client = GammaClient(session=session)

    markets = await client.fetch_markets()

    assert len(markets) == 1
    assert markets[0].near_certain_side is OutcomeSide.YES
    assert markets[0].volume == 550.0
    assert session.calls[0][1]["active"] == "true"
    assert session.calls[0][1]["closed"] == "false"
    assert session.calls[0][1]["archived"] == "false"


@pytest.mark.asyncio
async def test_fetch_all_open_markets_stops_when_full_page_contains_only_duplicates() -> None:
    session = FakeSession(
        [
            [
                _duplicate_page_payload("1", question="Will it rain tomorrow?"),
                _duplicate_page_payload("2", question="Will it snow tomorrow?"),
            ],
            [
                _duplicate_page_payload("1", question="Will it rain tomorrow?"),
                _duplicate_page_payload("2", question="Will it snow tomorrow?"),
            ],
        ]
    )
    client = GammaClient(session=session)

    markets = await client.fetch_all_open_markets(page_size=2)

    assert [market.market_id for market in markets] == ["1", "2"]
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_fetch_all_open_markets_enforces_max_pages_and_sorting() -> None:
    session = FakeSession(
        [
            [
                _market_payload("1"),
                _market_payload("2"),
            ],
            [
                _market_payload("3"),
                _market_payload("4"),
            ],
            [
                _market_payload("5"),
            ],
        ]
    )
    client = GammaClient(session=session)

    markets = await client.fetch_all_open_markets(page_size=2, max_pages=2, order="volume_24hr", ascending=False)

    assert [market.market_id for market in markets] == ["1", "2", "3", "4"]
    assert len(session.calls) == 2
    assert session.calls[0][1]["order"] == "volume_24hr"
    assert session.calls[0][1]["ascending"] == "false"


@pytest.mark.asyncio
async def test_fetch_tag_by_slug_returns_tag_payload() -> None:
    session = FakeSession(
        [
            {
                "id": "123",
                "slug": "crypto",
                "label": "Crypto",
            }
        ]
    )
    client = GammaClient(session=session)

    payload = await client.fetch_tag_by_slug("crypto")

    assert payload["id"] == "123"
    assert session.calls[0][0].endswith("/tags/slug/crypto")


def test_market_from_gamma_market_prefers_nested_category_and_volume_fallback() -> None:
    market = Market.from_gamma_market(
        {
            "id": "123",
            "question": "Will it rain tomorrow?",
            "conditionId": "0xabc",
            "endDate": "2026-04-06T18:00:00Z",
            "outcomePrices": '["0.90","0.10"]',
            "clobTokenIds": '["yes","no"]',
            "volumeNum": None,
            "volume": "1250.5",
            "events": [{"category": "sports"}],
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.volume == 1250.5
    assert market.category == "sports"


def test_market_from_gamma_market_extracts_primary_event_identity() -> None:
    market = Market.from_gamma_market(
        {
            "id": "123",
            "question": "Will it rain tomorrow?",
            "conditionId": "0xabc",
            "endDate": "2026-04-06T18:00:00Z",
            "outcomePrices": '["0.90","0.10"]',
            "clobTokenIds": '["yes","no"]',
            "volumeNum": None,
            "volume": "1250.5",
            "events": [
                {
                    "id": 42,
                    "slug": "weather-event",
                    "title": "Weather Event",
                }
            ],
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.event_id == "42"
    assert market.event_slug == "weather-event"
    assert market.event_title == "Weather Event"


def test_market_from_gamma_market_infers_sports_category_from_market_shape() -> None:
    market = Market.from_gamma_market(
        {
            "id": "sports-shape",
            "question": "Spread: Jaguares de Cordoba FC (-2.5)",
            "conditionId": "0xsports",
            "endDate": "2026-04-06T18:00:00Z",
            "outcomePrices": '["0.90","0.10"]',
            "clobTokenIds": '["yes","no"]',
            "volumeNum": None,
            "volume": "1500.0",
            "sportsMarketType": "spread",
            "line": "-2.5",
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.category == "sports"


def test_market_from_gamma_market_infers_crypto_category_from_text() -> None:
    market = Market.from_gamma_market(
        {
            "id": "crypto-shape",
            "question": "Ethereum Up or Down - April 6, 12:00PM-4:00PM ET",
            "conditionId": "0xcrypto",
            "endDate": "2026-04-06T18:00:00Z",
            "outcomePrices": '["0.90","0.10"]',
            "clobTokenIds": '["yes","no"]',
            "volumeNum": None,
            "volume": "1500.0",
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.category == "crypto"


def test_market_from_gamma_market_leaves_non_crypto_up_or_down_unknown() -> None:
    market = Market.from_gamma_market(
        {
            "id": "equity-shape",
            "question": "Meta (META) Up or Down on April 6?",
            "conditionId": "0xequity",
            "endDate": "2026-04-06T18:00:00Z",
            "outcomePrices": '["0.90","0.10"]',
            "clobTokenIds": '["yes","no"]',
            "volumeNum": None,
            "volume": "1500.0",
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.category == "unknown"


def test_market_from_gamma_market_leaves_weather_market_unknown() -> None:
    market = Market.from_gamma_market(
        {
            "id": "weather-shape",
            "question": "Will the highest temperature in Tokyo be 21°C on April 7?",
            "conditionId": "0xweather",
            "endDate": "2026-04-07T18:00:00Z",
            "outcomePrices": '["0.90","0.10"]',
            "clobTokenIds": '["yes","no"]',
            "volumeNum": None,
            "volume": "1500.0",
            "line": "21",
            "gameStartTime": "2026-04-06 15:00:00+00",
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.category == "unknown"


def test_market_from_gamma_market_infers_sports_from_league_text() -> None:
    market = Market.from_gamma_market(
        {
            "id": "league-sports",
            "question": "Will the Philadelphia 76ers make the NBA Playoffs?",
            "conditionId": "0xleague",
            "endDate": "2026-04-06T18:00:00Z",
            "outcomePrices": '["0.90","0.10"]',
            "clobTokenIds": '["yes","no"]',
            "volumeNum": None,
            "volume": "1500.0",
            "description": "This market will resolve to Yes if the listed team makes the 2025-26 NBA Playoffs.",
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.category == "sports"


def test_market_from_gamma_market_infers_sports_from_resolution_source() -> None:
    market = Market.from_gamma_market(
        {
            "id": "source-sports",
            "question": "Will Dyson Daniels lead the NBA in steals during the 2025-26 NBA season?",
            "conditionId": "0xsource",
            "endDate": "2026-04-06T18:00:00Z",
            "outcomePrices": '["0.90","0.10"]',
            "clobTokenIds": '["yes","no"]',
            "volumeNum": None,
            "volume": "1500.0",
            "resolutionSource": "https://www.nba.com/stats",
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.category == "sports"


def _duplicate_page_payload(market_id: str, *, question: str) -> dict[str, object]:
    return {
        "id": market_id,
        "question": question,
        "conditionId": f"0x{market_id}",
        "endDate": "2026-04-06T18:00:00Z",
        "outcomePrices": '["0.90","0.10"]',
        "clobTokenIds": '["yes","no"]',
        "volumeNum": None,
        "volume": "550.0",
        "active": True,
        "closed": False,
        "archived": False,
    }


def test_market_from_gamma_market_infers_sports_from_event_text() -> None:
    market = Market.from_gamma_market(
        {
            "id": "event-sports",
            "question": "Will Scottie Scheffler win the 2026 Masters tournament?",
            "conditionId": "0xevent",
            "endDate": "2026-04-06T18:00:00Z",
            "outcomePrices": '["0.90","0.10"]',
            "clobTokenIds": '["yes","no"]',
            "volumeNum": None,
            "volume": "1500.0",
            "events": [
                {
                    "title": "2026 Masters Tournament Winner",
                    "description": "Golf outright market for the Masters tournament.",
                    "resolutionSource": "https://www.masters.com",
                }
            ],
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.category == "sports"


def test_market_from_gamma_market_leaves_entertainment_unknown() -> None:
    market = Market.from_gamma_market(
        {
            "id": "entertainment-shape",
            "question": "Will \"Anaconda\" be the #2 global Netflix movie this week?",
            "conditionId": "0xent",
            "endDate": "2026-04-06T18:00:00Z",
            "outcomePrices": '["0.90","0.10"]',
            "clobTokenIds": '["yes","no"]',
            "volumeNum": None,
            "volume": "1500.0",
            "description": "Resolves based on Netflix top global movie rankings.",
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.category == "unknown"


def test_market_from_gamma_market_leaves_equity_unknown() -> None:
    market = Market.from_gamma_market(
        {
            "id": "equity-close-shape",
            "question": "Will Meta (META) close above $570 on April 6?",
            "conditionId": "0xmeta",
            "endDate": "2026-04-06T18:00:00Z",
            "outcomePrices": '["0.90","0.10"]',
            "clobTokenIds": '["yes","no"]',
            "volumeNum": None,
            "volume": "1500.0",
            "description": "This market resolves based on the official stock close above $570.",
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.category == "unknown"


@pytest.mark.asyncio
async def test_fetch_market_returns_single_market() -> None:
    session = FakeSession(
        [
            {
                "id": "123",
                "question": "Will it rain tomorrow?",
                "conditionId": "0xabc",
                "endDate": "2026-04-06T18:00:00Z",
                "outcomePrices": '["0.45","0.55"]',
                "clobTokenIds": '["yes","no"]',
                "volume": "550.0",
                "active": True,
                "closed": False,
                "archived": False,
            }
        ]
    )
    client = GammaClient(session=session)

    market = await client.fetch_market("123")

    assert market.market_id == "123"
    assert session.calls[0][0].endswith("/markets/123")


@pytest.mark.asyncio
async def test_fetch_market_by_slug_returns_first_match() -> None:
    session = FakeSession([[ _market_payload("123") ]])
    client = GammaClient(session=session)

    market = await client.fetch_market_by_slug("some-slug")

    assert market.market_id == "123"
    assert session.calls[0][1]["slug"] == "some-slug"


@pytest.mark.asyncio
async def test_fetch_all_open_markets_paginates_until_short_page() -> None:
    session = FakeSession(
        [
            [
                _market_payload("1"),
                _market_payload("2"),
            ],
            [
                _market_payload("3"),
            ],
        ]
    )
    client = GammaClient(session=session)

    markets = await client.fetch_all_open_markets(page_size=2)

    assert [market.market_id for market in markets] == ["1", "2", "3"]
    assert session.calls[0][1]["offset"] == 0
    assert session.calls[1][1]["offset"] == 2


@pytest.mark.asyncio
async def test_fetch_all_open_markets_uses_raw_page_length_not_parsed_count() -> None:
    session = FakeSession(
        [
            [
                _market_payload("1"),
                {
                    "id": "bad",
                    "question": "Bad row",
                    "conditionId": "0xbad",
                    "endDate": "2026-04-06T18:00:00Z",
                    "outcomePrices": None,
                    "clobTokenIds": None,
                    "volume": None,
                    "active": True,
                    "closed": False,
                    "archived": False,
                },
            ],
            [
                _market_payload("2"),
            ],
        ]
    )
    client = GammaClient(session=session)

    markets = await client.fetch_all_open_markets(page_size=2)

    assert [market.market_id for market in markets] == ["1", "2"]
    assert session.calls[0][1]["offset"] == 0
    assert session.calls[1][1]["offset"] == 2


@pytest.mark.asyncio
async def test_fetch_all_open_markets_deduplicates_market_ids_across_pages() -> None:
    session = FakeSession(
        [
            [
                _market_payload("1"),
                _market_payload("2"),
            ],
            [
                _market_payload("2"),
                _market_payload("3"),
            ],
            [
                _market_payload("4"),
            ],
        ]
    )
    client = GammaClient(session=session)

    markets = await client.fetch_all_open_markets(page_size=2)

    assert [market.market_id for market in markets] == ["1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_fetch_markets_raises_on_http_error() -> None:
    session = FakeSession([{"error": "bad request"}], statuses=[500])
    client = GammaClient(session=session)

    with pytest.raises(GammaAPIError, match="Gamma API request failed"):
        await client.fetch_markets()


@pytest.mark.asyncio
async def test_fetch_markets_skips_malformed_market_rows() -> None:
    session = FakeSession(
        [
            [
                _market_payload("valid"),
                {
                    "id": "bad",
                    "question": "Bad row",
                    "conditionId": "0xbad",
                    "endDate": "2026-04-06T18:00:00Z",
                    "outcomePrices": None,
                    "clobTokenIds": None,
                    "volume": None,
                    "active": True,
                    "closed": False,
                    "archived": False,
                },
            ]
        ]
    )
    client = GammaClient(session=session)

    markets = await client.fetch_markets()

    assert [market.market_id for market in markets] == ["valid"]


def _market_payload(market_id: str) -> dict[str, object]:
    return {
        "id": market_id,
        "question": f"Question {market_id}",
        "conditionId": f"0x{market_id}",
        "endDate": "2026-04-06T18:00:00Z",
        "outcomePrices": '["0.90","0.10"]',
        "clobTokenIds": '["yes","no"]',
        "volume": "550.0",
        "active": True,
        "closed": False,
        "archived": False,
    }
