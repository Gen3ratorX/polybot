from __future__ import annotations

from datetime import UTC, datetime

from bot.candidate_pool import (
    apply_candidate_caps,
    build_candidate_clusters,
    build_template_clusters,
    distinct_ranked_candidates,
)
from bot.ranker import RankedMarket
from models import Market, OutcomeSide


def test_distinct_ranked_candidates_keeps_best_per_event_cluster() -> None:
    ranked = [
        _ranked_market("a", event_id="evt-1", score=9.2),
        _ranked_market("b", event_id="evt-1", score=8.9),
        _ranked_market("c", event_id="evt-2", score=8.7),
    ]

    distinct = distinct_ranked_candidates(ranked)

    assert [item.market.market_id for item in distinct] == ["a", "c"]


def test_build_candidate_clusters_groups_related_markets() -> None:
    ranked = [
        _ranked_market("a", event_id="evt-1", score=9.2),
        _ranked_market("b", event_id="evt-1", score=8.9),
        _ranked_market("c", event_id="evt-2", score=8.7),
    ]

    clusters = build_candidate_clusters(ranked)

    assert clusters[0].cluster_key == "event:evt-1"
    assert clusters[0].size == 2
    assert clusters[0].market_ids == ("a", "b")


def test_build_template_clusters_groups_markets_by_template() -> None:
    ranked = [
        _ranked_market(
            "a",
            event_id="evt-1",
            score=9.2,
            question="Will Bitcoin reach $72,000 on April 6?",
        ),
        _ranked_market(
            "b",
            event_id="evt-2",
            score=8.9,
            question="Will Ethereum reach $2,250 on April 6?",
        ),
    ]

    clusters = build_template_clusters(ranked)

    assert clusters[0].cluster_key == "template:reach-threshold"
    assert clusters[0].size == 2


def test_apply_candidate_caps_limits_template_cluster_to_top_three() -> None:
    ranked = [
        _ranked_market(
            market_id,
            event_id=f"evt-{market_id}",
            score=score,
            question="Will Bitcoin reach $72,000 on April 6?",
        )
        for market_id, score in [("a", 9.4), ("b", 9.1), ("c", 8.9), ("d", 8.7)]
    ]

    capped = apply_candidate_caps(ranked, max_per_event=2, max_per_template=3)

    assert [item.market.market_id for item in capped] == ["a", "b", "c"]


def test_apply_candidate_caps_limits_event_cluster_to_top_two() -> None:
    ranked = [
        _ranked_market("a", event_id="evt-1", score=9.4),
        _ranked_market("b", event_id="evt-1", score=9.1),
        _ranked_market("c", event_id="evt-1", score=8.9),
        _ranked_market("d", event_id="evt-2", score=8.7),
    ]

    capped = apply_candidate_caps(ranked, max_per_event=2, max_per_template=3)

    assert [item.market.market_id for item in capped] == ["a", "b", "d"]


def test_apply_candidate_caps_limits_exact_score_event_to_one() -> None:
    ranked = [
        _ranked_market(
            "a",
            event_id="evt-1",
            score=9.4,
            question="Exact Score: Real Madrid CF 0 - 0 FC Bayern München?",
            event_title="Real Madrid CF vs. FC Bayern München - Exact Score",
        ),
        _ranked_market(
            "b",
            event_id="evt-1",
            score=9.1,
            question="Exact Score: Real Madrid CF 3 - 3 FC Bayern München?",
            event_title="Real Madrid CF vs. FC Bayern München - Exact Score",
        ),
        _ranked_market(
            "c",
            event_id="evt-2",
            score=8.9,
            question="Spread: Al Kholood Saudi Club (-1.5)",
            event_title="Al Hilal Saudi Club vs. Al Kholood Saudi Club - More Markets",
        ),
    ]

    capped = apply_candidate_caps(
        ranked,
        max_per_event=2,
        max_per_template=3,
        exact_score_max_per_event=1,
    )

    assert [item.market.market_id for item in capped] == ["a", "c"]


def _ranked_market(
    market_id: str,
    *,
    event_id: str,
    score: float,
    question: str | None = None,
    event_title: str | None = None,
) -> RankedMarket:
    market = Market(
        market_id=market_id,
        condition_id=f"cond-{market_id}",
        question=question or f"Question {market_id}",
        end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
        yes_token_id=f"yes-{market_id}",
        no_token_id=f"no-{market_id}",
        yes_price=0.91,
        no_price=0.06,
        volume=1000.0,
        category="sports",
        event_id=event_id,
        event_title=event_title or f"Event {event_id}",
    )
    return RankedMarket(
        market=market,
        score=score,
        selected_side=OutcomeSide.YES,
        selected_price=0.91,
        hours_to_close=3.0,
    )
