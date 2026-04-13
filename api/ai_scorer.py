from __future__ import annotations

from dataclasses import dataclass

from bot.config import AIScoringConfig
from models import Market


@dataclass(frozen=True, slots=True)
class AIScoreResult:
    rules_score: float
    ai_score: float
    blended_score: float
    source: str
    rationale: str


class AIScorer:
    def __init__(self, config: AIScoringConfig, *, api_key: str | None = None) -> None:
        self.config = config
        self.api_key = api_key

    def should_score(self, rules_score: float) -> bool:
        return (
            self.config.enabled
            and self.config.min_rules_score_to_use_ai <= rules_score <= self.config.max_rules_score_to_use_ai
        )

    def score_market(self, market: Market, *, rules_score: float) -> AIScoreResult | None:
        if not self.should_score(rules_score):
            return None

        ai_score, rationale = self._heuristic_score(market, rules_score)
        blended = round((rules_score * 0.8) + (ai_score * 0.2), 2)
        return AIScoreResult(
            rules_score=round(rules_score, 2),
            ai_score=ai_score,
            blended_score=min(max(blended, 0.0), 10.0),
            source="heuristic_fallback",
            rationale=rationale,
        )

    def _heuristic_score(self, market: Market, rules_score: float) -> tuple[float, str]:
        adjusted = rules_score
        rationale: list[str] = []

        if market.volume_change_1h_pct is not None:
            if market.volume_change_1h_pct >= 15:
                adjusted += 0.5
                rationale.append("strong recent volume acceleration")
            elif market.volume_change_1h_pct <= -10:
                adjusted -= 0.4
                rationale.append("recent volume dropped")

        if market.one_hour_price_change is not None:
            if abs(market.one_hour_price_change) >= 0.03:
                adjusted -= 0.25
                rationale.append("price moved quickly in the last hour")
            else:
                adjusted += 0.15
                rationale.append("price stayed stable in the last hour")

        if market.liquidity is not None:
            if market.liquidity >= 25_000:
                adjusted += 0.2
                rationale.append("deep liquidity")
            elif market.liquidity < 2_500:
                adjusted -= 0.2
                rationale.append("thin liquidity")

        if market.category.lower() in {"sports", "crypto"}:
            adjusted += 0.1
            rationale.append("category tends to resolve cleanly")

        score = round(min(max(adjusted, 0.0), 10.0), 2)
        if not rationale:
            rationale.append("no additional AI-side adjustment signals")
        return score, "; ".join(rationale)
