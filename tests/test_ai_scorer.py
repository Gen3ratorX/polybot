from __future__ import annotations

from datetime import UTC, datetime

from api.ai_scorer import AIScorer
from bot.config import AIScoringConfig
from models import Market


def test_ai_scorer_only_scores_inside_config_window() -> None:
    scorer = AIScorer(
        AIScoringConfig(
            enabled=True,
            min_rules_score_to_use_ai=6.0,
            max_rules_score_to_use_ai=7.5,
            max_daily_ai_calls=30,
        )
    )
    market = _market()

    assert scorer.score_market(market, rules_score=5.9) is None
    assert scorer.score_market(market, rules_score=7.6) is None


def test_ai_scorer_returns_blended_heuristic_result() -> None:
    scorer = AIScorer(
        AIScoringConfig(
            enabled=True,
            min_rules_score_to_use_ai=6.0,
            max_rules_score_to_use_ai=7.5,
            max_daily_ai_calls=30,
        )
    )

    result = scorer.score_market(_market(), rules_score=6.8)

    assert result is not None
    assert result.source == "heuristic_fallback"
    assert 0 <= result.ai_score <= 10
    assert 0 <= result.blended_score <= 10
    assert "volume" in result.rationale or "price" in result.rationale or "liquidity" in result.rationale


def _market() -> Market:
    return Market(
        market_id="540816",
        condition_id="0xcondition",
        question="Question",
        end_date=datetime(2026, 4, 6, 18, 0, tzinfo=UTC),
        yes_token_id="yes-token",
        no_token_id="no-token",
        yes_price=0.91,
        no_price=0.06,
        volume=2500,
        category="sports",
        liquidity=30000,
        volume_change_1h_pct=18.0,
        one_hour_price_change=0.01,
    )
