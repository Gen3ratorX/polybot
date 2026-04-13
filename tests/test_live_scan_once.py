from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.executor import LiveOrderPreview
from bot.ranker import RankedMarket
from models import Market, OutcomeSide
from bot.ranker import EdgeRanker
from bot.config import load_runtime_config
from bot.scanner import ScanDecision
from scripts.live_scan_once import _near_miss_candidates, _ranked_market_to_dict


def test_ranked_market_to_dict_exposes_trade_relevant_fields() -> None:
    market = Market(
        market_id="540816",
        condition_id="0xcondition",
        question="Question",
        end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_price=0.53,
        no_price=0.47,
        volume=1000,
        category="sports",
        slug="question-slug",
    )
    ranked = RankedMarket(
        market=market,
        score=8.7,
        selected_side=OutcomeSide.YES,
        selected_price=0.53,
        hours_to_close=3.2,
    )

    payload = _ranked_market_to_dict(ranked)

    assert payload["market_id"] == "540816"
    assert payload["slug"] == "question-slug"
    assert payload["selected_side"] == "YES"
    assert payload["score"] == 8.7


def test_live_order_preview_remains_json_ready() -> None:
    preview = LiveOrderPreview(
        market_id="540816",
        question="Question",
        outcome_side="YES",
        token_id="yes-token",
        requested_budget_usdc=3.0,
        budget_usdc=3.0,
        limit_price=0.53,
        shares=5.660377,
        midpoint=0.535,
        best_bid=0.53,
        best_ask=0.54,
        tick_size=0.01,
        min_order_size=5.0,
        minimum_budget_usdc=2.65,
    )

    payload = preview.as_dict()

    assert payload["budget_usdc"] == 3.0
    assert payload["minimum_budget_usdc"] == 2.65


def test_near_miss_candidates_prioritize_fewer_failures_then_score() -> None:
    config = load_runtime_config("config.yaml")
    ranker = EdgeRanker(config.strategy)
    strong_fail = ScanDecision(
        market=Market(
            market_id="a",
            condition_id="cond-a",
            question="A",
            end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
            yes_token_id="yes-a",
            no_token_id="no-a",
            yes_price=0.95,
            no_price=0.03,
            volume=1000,
            category="sports",
        ),
        qualifies=False,
        hours_to_close=3.0,
        reasons=("spread",),
    )
    weaker_fail = ScanDecision(
        market=Market(
            market_id="b",
            condition_id="cond-b",
            question="B",
            end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
            yes_token_id="yes-b",
            no_token_id="no-b",
            yes_price=0.90,
            no_price=0.04,
            volume=1000,
            category="sports",
        ),
        qualifies=False,
        hours_to_close=3.0,
        reasons=("price", "spread"),
    )

    items = _near_miss_candidates([weaker_fail, strong_fail], ranker, top=2)

    assert [item["market_id"] for item in items] == ["a", "b"]
