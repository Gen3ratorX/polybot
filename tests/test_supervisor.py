from __future__ import annotations

from datetime import UTC, datetime

from api.ai_scorer import AIScorer
from bot.config import AIScoringConfig, load_runtime_config
from bot.ranker import RankedMarket
from bot.supervisor import (
    PreparedCycle,
    determine_live_budget,
    distinct_scored_candidates,
    evaluate_candidates,
    has_open_position_capacity,
)
from bot.tracker import TradeTracker
from models import Market, OutcomeSide


def test_determine_live_budget_rejects_below_bankroll_floor() -> None:
    runtime = load_runtime_config("config.yaml")

    budget = determine_live_budget(
        bankroll=10.764913,
        runtime=runtime,
        requested_budget=3.0,
    )

    assert budget == 0.0


def test_determine_live_budget_uses_kelly_then_caps_to_ceiling() -> None:
    runtime = load_runtime_config("config.yaml")

    budget = determine_live_budget(
        bankroll=31.921998,
        runtime=runtime,
        requested_budget=5.0,
    )

    assert budget == 4.7883


def test_determine_live_budget_caps_to_execution_and_autoscale_limits() -> None:
    runtime = load_runtime_config("config.yaml")

    budget = determine_live_budget(
        bankroll=100.0,
        runtime=runtime,
        requested_budget=20.0,
    )

    assert budget == 3.0


def test_evaluate_candidates_uses_ai_blended_score_for_ordering() -> None:
    ai_scorer = AIScorer(
        AIScoringConfig(
            enabled=True,
            min_rules_score_to_use_ai=6.0,
            max_rules_score_to_use_ai=7.5,
            max_daily_ai_calls=30,
        )
    )
    ranked = [
        RankedMarket(
            market=_market("a", volume_change=20.0, liquidity=30000.0),
            score=7.1,
            selected_side=OutcomeSide.YES,
            selected_price=0.91,
            hours_to_close=3.0,
        ),
        RankedMarket(
            market=_market("b", volume_change=-12.0, liquidity=1000.0),
            score=7.2,
            selected_side=OutcomeSide.YES,
            selected_price=0.91,
            hours_to_close=3.0,
        ),
    ]

    candidates = evaluate_candidates(ranked, ai_scorer=ai_scorer, min_score=7.0)

    assert [item.ranked_market.market.market_id for item in candidates] == ["a", "b"]
    assert candidates[0].ai_result is not None


def test_prepared_cycle_as_dict_exposes_skip_reason_without_preview() -> None:
    prepared = PreparedCycle(
        state_id=1,
        bankroll=10.0,
        phase=0,
        budget_cap=0.0,
        selected_budget=0.0,
        kill_signal=None,
        skip_reason="Current bankroll phase does not permit supervised live positions yet.",
        reconciled_positions=(),
        top_candidates=(),
        near_miss_candidates=(),
        preview=None,
    )

    payload = prepared.as_dict()

    assert payload["budget_cap"] == 0.0
    assert payload["skip_reason"] == "Current bankroll phase does not permit supervised live positions yet."
    assert payload["preview"] is None
    assert payload["raw_candidate_count"] == 0
    assert payload["distinct_candidate_count"] == 0
    assert payload["capped_candidate_count"] == 0


def test_distinct_scored_candidates_limits_to_one_per_event_cluster() -> None:
    ai_scorer = AIScorer(
        AIScoringConfig(
            enabled=False,
            min_rules_score_to_use_ai=6.0,
            max_rules_score_to_use_ai=7.5,
            max_daily_ai_calls=30,
        )
    )
    ranked = [
        RankedMarket(
            market=_market("a", volume_change=20.0, liquidity=30000.0, event_id="evt-1"),
            score=8.0,
            selected_side=OutcomeSide.YES,
            selected_price=0.91,
            hours_to_close=3.0,
        ),
        RankedMarket(
            market=_market("b", volume_change=10.0, liquidity=25000.0, event_id="evt-1"),
            score=7.9,
            selected_side=OutcomeSide.YES,
            selected_price=0.91,
            hours_to_close=3.0,
        ),
        RankedMarket(
            market=_market("c", volume_change=5.0, liquidity=20000.0, event_id="evt-2"),
            score=7.8,
            selected_side=OutcomeSide.YES,
            selected_price=0.91,
            hours_to_close=3.0,
        ),
    ]

    candidates = evaluate_candidates(ranked, ai_scorer=ai_scorer, min_score=7.0)
    distinct = distinct_scored_candidates(candidates)

    assert [item.ranked_market.market.market_id for item in distinct] == ["a", "c"]


def test_has_open_position_capacity_respects_execution_cap(tmp_path) -> None:
    runtime = load_runtime_config("config.yaml")
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'cap.db'}")
    tracker.initialize()

    assert has_open_position_capacity(tracker=tracker, runtime=runtime) is True

    for index in range(runtime.execution.max_open_positions):
        trade = _filled_trade(f"cap-trade-{index}")
        tracker.record_trade(trade)
        tracker.register_open_position_from_trade(trade)

    assert has_open_position_capacity(tracker=tracker, runtime=runtime) is False


def _market(market_id: str, *, volume_change: float, liquidity: float, event_id: str | None = None) -> Market:
    return Market(
        market_id=market_id,
        condition_id=f"cond-{market_id}",
        question=f"Question {market_id}",
        end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
        yes_token_id=f"yes-{market_id}",
        no_token_id=f"no-{market_id}",
        yes_price=0.91,
        no_price=0.06,
        volume=1000,
        category="sports",
        event_id=event_id,
        volume_change_1h_pct=volume_change,
        one_hour_price_change=0.01,
        liquidity=liquidity,
    )


def _filled_trade(market_id: str):
    from datetime import timedelta
    from models import Trade, TradeOutcome

    timestamp = datetime(2026, 4, 6, 12, 0, tzinfo=UTC)
    return Trade(
        timestamp=timestamp,
        market_id=market_id,
        market_question=f"Question {market_id}",
        category="sports",
        side=OutcomeSide.YES,
        entry_price=0.91,
        position_size=5.0,
        score=7.5,
        hours_to_close=3.0,
        order_id=f"oid-{market_id}",
        fill_price=0.91,
        fill_time=timestamp + timedelta(seconds=5),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
