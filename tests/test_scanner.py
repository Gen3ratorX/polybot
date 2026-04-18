from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from api.catalyst import CatalystEvent, CatalystSnapshot
from api.spot import SpotSnapshot
from bot.config import load_runtime_config
from bot.scanner import MarketScanner
from models import Market


class StubGammaClient:
    def __init__(self, markets: list[Market]) -> None:
        self.markets = markets
        self.calls = 0

    async def fetch_all_open_markets(self) -> list[Market]:
        self.calls += 1
        return self.markets


ROOT_CONFIG = load_runtime_config("config.yaml")


def test_scanner_filters_gate_1_rules() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    scanner = MarketScanner(
        gamma_client=StubGammaClient([]), config=ROOT_CONFIG.strategy
    )

    valid_market = _market(
        "valid",
        end_date=reference + timedelta(hours=4),
        yes_price=0.92,
        no_price=0.06,
        volume=1200,
    )
    too_late = _market(
        "too-late",
        end_date=reference + timedelta(hours=7),
        yes_price=0.92,
        no_price=0.06,
        volume=1200,
    )
    too_low_volume = _market(
        "low-volume",
        end_date=reference + timedelta(hours=4),
        yes_price=0.92,
        no_price=0.06,
        volume=499,
    )
    no_qualified_price = _market(
        "price-miss",
        end_date=reference + timedelta(hours=4),
        yes_price=0.80,
        no_price=0.19,
        volume=1200,
    )

    result = scanner.filter_markets(
        [valid_market, too_late, too_low_volume, no_qualified_price],
        as_of=reference,
    )

    assert [market.market_id for market in result] == ["valid"]


def test_scanner_allows_sports_prices_up_to_sports_specific_cap() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    research_config = load_runtime_config("config.yaml", strategy_section="research_strategy")
    scanner = MarketScanner(
        gamma_client=StubGammaClient([]), config=research_config.strategy
    )
    sports_market = _market(
        "sports-borderline",
        end_date=reference + timedelta(hours=4),
        yes_price=0.02,
        no_price=0.972,
        volume=1_500,
        category="sports",
    )
    non_sports_market = _market(
        "crypto-borderline",
        end_date=reference + timedelta(hours=4),
        yes_price=0.02,
        no_price=0.972,
        volume=1_500,
        category="crypto",
    )

    sports_decision = scanner.diagnose_market(sports_market, as_of=reference)
    non_sports_decision = scanner.diagnose_market(non_sports_market, as_of=reference)

    assert sports_decision.qualifies is True
    assert non_sports_decision.qualifies is False
    assert "price" in non_sports_decision.reasons


def test_scanner_diagnose_market_lists_failed_rules() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    scanner = MarketScanner(
        gamma_client=StubGammaClient([]), config=ROOT_CONFIG.strategy
    )
    market = _market(
        "bad",
        end_date=reference - timedelta(minutes=30),
        yes_price=0.80,
        no_price=0.25,
        volume=100,
        active=False,
    )

    decision = scanner.diagnose_market(market, as_of=reference)

    assert decision.qualifies is False
    assert set(decision.reasons) == {"status", "time_past_close", "volume", "price"}


def test_scanner_rejects_qualitative_and_flow_shock_markets() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    scanner = MarketScanner(
        gamma_client=StubGammaClient([]), config=ROOT_CONFIG.strategy
    )
    market = _market(
        "qualitative",
        end_date=reference + timedelta(hours=4),
        yes_price=0.93,
        no_price=0.05,
        volume=1200,
    )
    market = Market(
        market_id=market.market_id,
        condition_id=market.condition_id,
        question="Will CEO tweet about the merger?",
        slug="ceo-tweet-merger",
        end_date=market.end_date,
        yes_token_id=market.yes_token_id,
        no_token_id=market.no_token_id,
        yes_price=market.yes_price,
        no_price=market.no_price,
        volume=market.volume,
        category="unknown",
        active=market.active,
        closed=market.closed,
        archived=market.archived,
        volume_change_1h_pct=24.0,
        one_hour_price_change=0.05,
        liquidity=1200.0,
    )

    decision = scanner.diagnose_market(market, as_of=reference)

    assert decision.qualifies is False
    assert "qualitative" in decision.reasons
    assert "flow_spike" in decision.reasons
    assert "price_shock" in decision.reasons
    assert "thin_liquidity" in decision.reasons


def test_scanner_rejects_volume_spike_using_native_flow_fields() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    scanner = MarketScanner(
        gamma_client=StubGammaClient([]), config=ROOT_CONFIG.strategy
    )
    market = _market(
        "flow-spike",
        end_date=reference + timedelta(hours=4),
        yes_price=0.93,
        no_price=0.05,
        volume=1200,
        volume_change_1h_pct=18.0,
        liquidity=5000.0,
        one_hour_price_change=0.01,
    )

    decision = scanner.diagnose_market(market, as_of=reference)

    assert decision.qualifies is False
    assert "flow_spike" in decision.reasons
    assert "price_shock" not in decision.reasons


def test_scanner_accepts_btc_momentum_markets_and_requires_asset_keywords() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    btc_config = load_runtime_config("config.yaml", strategy_section="btc_up_down")
    scanner = MarketScanner(
        gamma_client=StubGammaClient([]), config=btc_config.strategy
    )
    catalyst_snapshot = _btc_catalyst_snapshot(reference)
    qualifying = _market(
        "btc-momentum",
        end_date=reference + timedelta(hours=4),
        yes_price=0.56,
        no_price=0.44,
        volume=8_000,
        category="crypto",
        liquidity=20_000.0,
        volume_change_1h_pct=28.0,
        one_hour_price_change=0.022,
    )
    missing_keyword = Market(
        market_id="btc-missing-keyword",
        condition_id="cond-btc-missing-keyword",
        question="Will ETH be above 4000?",
        slug="eth-above-4000",
        end_date=reference + timedelta(hours=4),
        yes_token_id="yes-btc-missing-keyword",
        no_token_id="no-btc-missing-keyword",
        yes_price=0.56,
        no_price=0.44,
        volume=8_000,
        category="crypto",
        liquidity=20_000.0,
        volume_change_1h_pct=28.0,
        one_hour_price_change=0.022,
    )
    low_momentum = _market(
        "btc-low-momentum",
        end_date=reference + timedelta(hours=4),
        yes_price=0.56,
        no_price=0.44,
        volume=8_000,
        category="crypto",
        liquidity=20_000.0,
        volume_change_1h_pct=3.0,
        one_hour_price_change=0.004,
    )

    qualifying_decision = scanner.diagnose_market(
        qualifying,
        as_of=reference,
        catalyst_snapshot=catalyst_snapshot,
    )
    missing_keyword_decision = scanner.diagnose_market(
        missing_keyword,
        as_of=reference,
        catalyst_snapshot=catalyst_snapshot,
    )
    low_momentum_decision = scanner.diagnose_market(
        low_momentum,
        as_of=reference,
        catalyst_snapshot=catalyst_snapshot,
    )

    assert qualifying_decision.qualifies is True
    assert missing_keyword_decision.qualifies is False
    assert "asset_keyword" in missing_keyword_decision.reasons
    assert low_momentum_decision.qualifies is False
    assert "momentum_volume" in low_momentum_decision.reasons
    assert "momentum_price" in low_momentum_decision.reasons
    assert "catalyst_inactive" not in qualifying_decision.reasons


def test_scanner_rejects_btc_without_active_catalyst() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    btc_config = load_runtime_config("config.yaml", strategy_section="btc_up_down")
    scanner = MarketScanner(
        gamma_client=StubGammaClient([]), config=btc_config.strategy
    )
    market = _market(
        "btc-no-catalyst",
        end_date=reference + timedelta(hours=4),
        yes_price=0.56,
        no_price=0.44,
        volume=8_000,
        category="crypto",
        liquidity=20_000.0,
        volume_change_1h_pct=28.0,
        one_hour_price_change=0.022,
    )

    decision = scanner.diagnose_market(market, as_of=reference, catalyst_snapshot=None)

    assert decision.qualifies is False
    assert "catalyst_inactive" in decision.reasons


def test_scanner_allows_hourly_momentum_multi_asset_without_active_catalyst() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    scalp_config = load_runtime_config("config.yaml", strategy_section="hourly_momentum_multi_asset")
    scanner = MarketScanner(
        gamma_client=StubGammaClient([]), config=scalp_config.strategy
    )
    market = _market(
        "btc-scalp",
        end_date=reference + timedelta(minutes=42),
        yes_price=0.54,
        no_price=0.46,
        volume=12_000,
        category="crypto",
        question="Will Ethereum finish the hour above the range?",
        slug="ethereum-hourly-range",
        liquidity=30_000.0,
        volume_change_1h_pct=14.0,
        one_hour_price_change=0.01,
    )
    spot_snapshots = {
        "ETHUSD": SpotSnapshot(
            symbol="ETHUSD",
            pair="ETHUSD",
            spot_price=3400.0,
            return_15m_pct=0.004,
            return_1h_pct=0.02,
            fetch_latency_seconds=0.2,
            observed_at=reference,
            age_seconds=30.0,
            source="kraken",
            payload=None,
        )
    }

    decision = scanner.diagnose_market(
        market,
        as_of=reference,
        catalyst_snapshot=None,
        spot_snapshot=spot_snapshots,
    )

    assert decision.qualifies is True
    assert "catalyst_inactive" not in decision.reasons
    assert "spot_missing" not in decision.reasons


def test_scanner_uses_spot_snapshot_for_btc_momentum_signal() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    btc_config = load_runtime_config("config.yaml", strategy_section="btc_up_down")
    scanner = MarketScanner(
        gamma_client=StubGammaClient([]), config=btc_config.strategy
    )
    catalyst_snapshot = _btc_catalyst_snapshot(reference)
    market = _market(
        "btc-spot",
        end_date=reference + timedelta(hours=4),
        yes_price=0.56,
        no_price=0.44,
        volume=8_000,
        category="crypto",
        liquidity=20_000.0,
        volume_change_1h_pct=28.0,
        one_hour_price_change=0.016,
    )
    strong_snapshot = SpotSnapshot(
        symbol="XBTUSD",
        pair="XBTUSD",
        spot_price=70500.0,
        return_15m_pct=0.006,
        return_1h_pct=0.025,
        fetch_latency_seconds=0.2,
        observed_at=reference,
        age_seconds=30.0,
        source="kraken",
        payload=None,
    )
    stale_snapshot = SpotSnapshot(
        symbol="XBTUSD",
        pair="XBTUSD",
        spot_price=70500.0,
        return_15m_pct=0.006,
        return_1h_pct=0.025,
        fetch_latency_seconds=0.2,
        observed_at=reference - timedelta(minutes=5),
        age_seconds=300.0,
        source="kraken",
        payload=None,
    )

    strong_decision = scanner.diagnose_market(
        market,
        as_of=reference,
        spot_snapshot=strong_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )
    stale_decision = scanner.diagnose_market(
        market,
        as_of=reference,
        spot_snapshot=stale_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )

    assert strong_decision.qualifies is True
    assert stale_decision.qualifies is False
    assert "spot_stale" in stale_decision.reasons


@pytest.mark.asyncio
async def test_scanner_scan_uses_gamma_client() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    valid_market = _market(
        "valid",
        end_date=reference + timedelta(hours=4),
        yes_price=0.91,
        no_price=0.08,
        volume=1200,
    )
    client = StubGammaClient([valid_market])
    scanner = MarketScanner(gamma_client=client, config=ROOT_CONFIG.strategy)

    result = await scanner.scan(as_of=reference)

    assert [market.market_id for market in result] == ["valid"]
    assert client.calls == 1


def _market(
    market_id: str,
    *,
    end_date: datetime,
    yes_price: float,
    no_price: float,
    volume: float,
    category: str = "sports",
    question: str | None = None,
    slug: str | None = None,
    active: bool = True,
    closed: bool = False,
    archived: bool = False,
    liquidity: float | None = None,
    volume_change_1h_pct: float | None = None,
    one_hour_price_change: float | None = None,
) -> Market:
    return Market(
        market_id=market_id,
        condition_id=f"cond-{market_id}",
        question=question or f"Question {market_id}",
        end_date=end_date,
        yes_token_id=f"yes-{market_id}",
        no_token_id=f"no-{market_id}",
        yes_price=yes_price,
        no_price=no_price,
        volume=volume,
        category=category,
        slug=slug,
        liquidity=liquidity,
        volume_change_1h_pct=volume_change_1h_pct,
        one_hour_price_change=one_hour_price_change,
        active=active,
        closed=closed,
        archived=archived,
    )


def _btc_catalyst_snapshot(reference: datetime) -> CatalystSnapshot:
    event = CatalystEvent(
        calendar_id="fomc-1",
        country="United States",
        category="Federal Reserve",
        event="FOMC Press Conference",
        date=reference + timedelta(minutes=15),
        importance=3,
        source="Trading Economics",
        payload={"Event": "FOMC Press Conference"},
    )
    return CatalystSnapshot(
        provider="trading_economics",
        observed_at=reference,
        fetch_latency_seconds=0.1,
        events=(event,),
        active_events=(event,),
        payload={"event_count": 1, "active_event_count": 1},
    )
