from __future__ import annotations

from datetime import UTC, datetime, timedelta

from api.catalyst import CatalystEvent, CatalystSnapshot
from api.spot import SpotSnapshot
from bot.config import load_runtime_config
from bot.ranker import EdgeRanker
from models import Market, OutcomeSide


ROOT_CONFIG = load_runtime_config("config.yaml")


def test_ranker_scores_market_close_to_spec_formula() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    market = _market(
        market_id="sports-edge",
        category="sports",
        end_date=reference + timedelta(hours=2, minutes=30),
        yes_price=0.96,
        no_price=0.02,
        volume=2_000,
        volume_change_1h_pct=12,
    )
    ranker = EdgeRanker(ROOT_CONFIG.strategy)

    score = ranker.score_market(market, as_of=reference)

    assert score == 8.35


def test_ranker_defaults_missing_volume_trend_to_neutral() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    market = _market(
        market_id="neutral-trend",
        category="crypto",
        end_date=reference + timedelta(days=5),
        yes_price=0.90,
        no_price=0.05,
        volume=800,
        volume_change_1h_pct=None,
    )
    ranker = EdgeRanker(ROOT_CONFIG.strategy)

    score = ranker.score_market(market, as_of=reference)

    assert score == 4.46


def test_ranker_applies_category_multiplier_to_politics_markets() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    market = _market(
        market_id="politics-penalty",
        category="politics",
        end_date=reference + timedelta(hours=3),
        yes_price=0.96,
        no_price=0.02,
        volume=4_000,
        volume_change_1h_pct=12,
    )
    ranker = EdgeRanker(ROOT_CONFIG.strategy)

    score = ranker.score_market(market, as_of=reference)

    assert score == 6.26
    assert score < ROOT_CONFIG.strategy.min_score


def test_ranker_penalizes_flow_spikes_near_threshold() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    stable = _market(
        market_id="stable-flow",
        category="sports",
        end_date=reference + timedelta(hours=3),
        yes_price=0.95,
        no_price=0.03,
        volume=2_000,
        volume_change_1h_pct=4.0,
    )
    spiky = _market(
        market_id="spiky-flow",
        category="sports",
        end_date=reference + timedelta(hours=3),
        yes_price=0.95,
        no_price=0.03,
        volume=2_000,
        volume_change_1h_pct=14.5,
    )
    ranker = EdgeRanker(ROOT_CONFIG.strategy)

    stable_breakdown = ranker.score_breakdown(stable, as_of=reference)
    spiky_breakdown = ranker.score_breakdown(spiky, as_of=reference)

    assert spiky_breakdown.flow_score < stable_breakdown.flow_score
    assert spiky_breakdown.final_score < stable_breakdown.final_score
    assert stable_breakdown.final_score - spiky_breakdown.final_score >= 0.5


def test_ranker_scores_btc_momentum_markets_with_flow_as_signal() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    btc_config = load_runtime_config("config.yaml", strategy_section="btc_up_down")
    ranker = EdgeRanker(btc_config.strategy)
    catalyst_snapshot = _btc_catalyst_snapshot(reference)
    strong = _market(
        market_id="btc-strong",
        category="crypto",
        end_date=reference + timedelta(hours=4),
        yes_price=0.56,
        no_price=0.44,
        volume=8_000,
        volume_change_1h_pct=28.0,
        one_hour_price_change=0.022,
    )
    weak = _market(
        market_id="btc-weak",
        category="crypto",
        end_date=reference + timedelta(hours=4),
        yes_price=0.56,
        no_price=0.44,
        volume=8_000,
        volume_change_1h_pct=4.0,
        one_hour_price_change=0.004,
    )

    strong_breakdown = ranker.score_breakdown(
        strong,
        as_of=reference,
        catalyst_snapshot=catalyst_snapshot,
    )
    weak_breakdown = ranker.score_breakdown(
        weak,
        as_of=reference,
        catalyst_snapshot=catalyst_snapshot,
    )

    assert strong_breakdown.final_score > weak_breakdown.final_score
    assert strong_breakdown.flow_score > weak_breakdown.flow_score
    assert strong_breakdown.catalyst_score > 0
    assert strong_breakdown.final_score >= btc_config.strategy.min_score


def test_ranker_uses_spot_snapshot_to_reward_btc_momentum_lag() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    btc_config = load_runtime_config("config.yaml", strategy_section="btc_up_down")
    ranker = EdgeRanker(btc_config.strategy)
    catalyst_snapshot = _btc_catalyst_snapshot(reference)
    market = _market(
        market_id="btc-spot",
        category="crypto",
        end_date=reference + timedelta(hours=4),
        yes_price=0.56,
        no_price=0.44,
        volume=8_000,
        volume_change_1h_pct=18.0,
        one_hour_price_change=0.008,
    )
    strong_snapshot = SpotSnapshot(
        symbol="XBTUSD",
        pair="XBTUSD",
        spot_price=70500.0,
        return_15m_pct=0.006,
        return_1h_pct=0.02,
        fetch_latency_seconds=0.2,
        observed_at=reference,
        age_seconds=30.0,
        source="kraken",
        payload=None,
    )
    weak_snapshot = SpotSnapshot(
        symbol="XBTUSD",
        pair="XBTUSD",
        spot_price=70500.0,
        return_15m_pct=0.001,
        return_1h_pct=0.002,
        fetch_latency_seconds=0.2,
        observed_at=reference,
        age_seconds=30.0,
        source="kraken",
        payload=None,
    )

    strong_breakdown = ranker.score_breakdown(
        market,
        as_of=reference,
        spot_snapshot=strong_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )
    weak_breakdown = ranker.score_breakdown(
        market,
        as_of=reference,
        spot_snapshot=weak_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )

    assert strong_breakdown.final_score > weak_breakdown.final_score
    assert strong_breakdown.flow_score > weak_breakdown.flow_score


def test_ranker_score_breakdown_identifies_time_as_dominant_drag_for_far_market() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    market = _market(
        market_id="far-sports",
        category="sports",
        end_date=reference + timedelta(days=25),
        yes_price=0.97,
        no_price=0.01,
        volume=1_200,
        volume_change_1h_pct=None,
    )
    ranker = EdgeRanker(ROOT_CONFIG.strategy)

    breakdown = ranker.score_breakdown(market, as_of=reference)

    assert breakdown.final_score == 6.5
    assert breakdown.dominant_drag == "time"


def test_ranker_sorts_and_filters_below_threshold() -> None:
    reference = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    strong = _market(
        market_id="strong",
        category="sports",
        end_date=reference + timedelta(hours=4),
        yes_price=0.95,
        no_price=0.03,
        volume=1_500,
        volume_change_1h_pct=12,
    )
    borderline = _market(
        market_id="borderline",
        category="unknown",
        end_date=reference + timedelta(days=25),
        yes_price=0.86,
        no_price=0.12,
        volume=1_200,
        volume_change_1h_pct=None,
    )
    ranker = EdgeRanker(ROOT_CONFIG.strategy)

    ranked = ranker.rank_markets([borderline, strong], as_of=reference)

    assert [item.market.market_id for item in ranked] == ["strong"]
    assert ranked[0].selected_side is OutcomeSide.YES
    assert ranked[0].score >= ROOT_CONFIG.strategy.min_score


def _market(
    market_id: str,
    *,
    category: str,
    end_date: datetime,
    yes_price: float,
    no_price: float,
    volume: float,
    volume_change_1h_pct: float | None,
    one_hour_price_change: float | None = None,
) -> Market:
    return Market(
        market_id=market_id,
        condition_id=f"cond-{market_id}",
        question=f"Question {market_id}",
        end_date=end_date,
        yes_token_id=f"yes-{market_id}",
        no_token_id=f"no-{market_id}",
        yes_price=yes_price,
        no_price=no_price,
        volume=volume,
        category=category,
        volume_change_1h_pct=volume_change_1h_pct,
        one_hour_price_change=one_hour_price_change,
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
