from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from api.catalyst import CatalystSnapshot
from api.spot import SpotSnapshot
from bot.config import StrategyProfile
from models import Market, OutcomeSide


@dataclass(frozen=True, slots=True)
class RankedMarket:
    market: Market
    score: float
    selected_side: OutcomeSide
    selected_price: float
    hours_to_close: float


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    market_id: str
    category: str
    hours_to_close: float
    price_score: float
    time_score: float
    volume_score: float
    flow_score: float
    spread_score: float
    category_multiplier: float
    base_score: float
    final_score: float
    catalyst_score: float = 0.0

    @property
    def dominant_drag(self) -> str:
        drags = {
            "price": 3.0 - self.price_score,
            "time": 2.5 - self.time_score,
            "volume": 1.5 - self.volume_score,
            "flow": 1.5 - self.flow_score,
            "category": self.base_score * (1.0 - self.category_multiplier),
        }
        return max(drags.items(), key=lambda item: item[1])[0]


class EdgeRanker:
    def __init__(self, config: StrategyProfile) -> None:
        self.config = config

    def score_market(
        self,
        market: Market,
        *,
        as_of: datetime | None = None,
        spot_snapshot: SpotSnapshot | None = None,
        catalyst_snapshot: CatalystSnapshot | None = None,
    ) -> float:
        return self.score_breakdown(
            market,
            as_of=as_of,
            spot_snapshot=spot_snapshot,
            catalyst_snapshot=catalyst_snapshot,
        ).final_score

    def score_breakdown(
        self,
        market: Market,
        *,
        as_of: datetime | None = None,
        spot_snapshot: SpotSnapshot | None = None,
        catalyst_snapshot: CatalystSnapshot | None = None,
    ) -> ScoreBreakdown:
        reference = as_of or datetime.now(UTC)
        hours = market.hours_to_close(reference)
        price = market.near_certain_price
        mode = self.config.signal_mode

        if mode == "momentum":
            price_score = self._momentum_price_score(price)
            time_score = self._momentum_time_score(hours)
            volume_score = self._momentum_volume_score(market, spot_snapshot=spot_snapshot)
            flow_score = self._momentum_flow_score(market, spot_snapshot=spot_snapshot)
            catalyst_score = self._momentum_catalyst_score(catalyst_snapshot, as_of=reference)
        else:
            max_price = self._max_price_for_market(market)
            price_score = self._price_score(price, max_price=max_price)
            time_score = self._time_score(hours)
            volume_score = self._volume_score(market.volume_change_1h_pct)
            flow_score = self._flow_score(market)
            catalyst_score = 0.0
        spread_score = self._spread_score()
        category_multiplier = self._category_multiplier(market.category)

        base_score = price_score + time_score + volume_score + flow_score + spread_score + catalyst_score
        total = round(min(base_score * category_multiplier, 10.0), 2)
        return ScoreBreakdown(
            market_id=market.market_id,
            category=market.category,
            hours_to_close=round(hours, 4),
            price_score=round(price_score, 4),
            time_score=round(time_score, 4),
            volume_score=round(volume_score, 4),
            flow_score=round(flow_score, 4),
            spread_score=round(spread_score, 4),
            category_multiplier=round(category_multiplier, 4),
            base_score=round(base_score, 4),
            final_score=total,
            catalyst_score=round(catalyst_score, 4),
        )

    def rank_markets(
        self,
        markets: list[Market],
        *,
        as_of: datetime | None = None,
        spot_snapshot: SpotSnapshot | None = None,
        catalyst_snapshot: CatalystSnapshot | None = None,
    ) -> list[RankedMarket]:
        reference = as_of or datetime.now(UTC)
        ranked: list[RankedMarket] = []

        for market in markets:
            score = self.score_market(
                market,
                as_of=reference,
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
            )
            if score < self.config.min_score:
                continue
            ranked.append(
                RankedMarket(
                    market=market,
                    score=score,
                    selected_side=market.near_certain_side,
                    selected_price=market.near_certain_price,
                    hours_to_close=round(market.hours_to_close(reference), 4),
                )
            )

        return sorted(ranked, key=lambda item: item.score, reverse=True)

    def _price_score(self, price: float, *, max_price: float) -> float:
        clamped = min(max(price, self.config.min_price), max_price)
        return ((clamped - self.config.min_price) / (max_price - self.config.min_price)) * 3

    def _momentum_price_score(self, price: float) -> float:
        center = self.config.momentum_price_center or 0.5
        width = max(self.config.momentum_price_width or 0.1, 1e-6)
        distance = abs(price - center)
        if distance >= width:
            return 0.0
        return ((1.0 - (distance / width)) * 3.0)

    def _max_price_for_market(self, market: Market) -> float:
        if market.category.lower() == "sports":
            return self.config.sports_max_price
        return self.config.max_price

    def _time_score(self, hours_to_close: float) -> float:
        if hours_to_close <= 6:
            return 2.5
        if hours_to_close <= 24:
            return 2.0
        if hours_to_close <= 72:
            return 1.5
        if hours_to_close <= 24 * 7:
            return 1.0
        if hours_to_close <= 24 * 30:
            return 0.5
        return 0.2

    def _momentum_time_score(self, hours_to_close: float) -> float:
        if hours_to_close <= 1:
            return 2.5
        if hours_to_close <= 4:
            return 2.25
        if hours_to_close <= 12:
            return 2.0
        if hours_to_close <= 24:
            return 1.25
        if hours_to_close <= 72:
            return 0.75
        return 0.25

    def _category_multiplier(self, category: str) -> float:
        return self.config.category_weights.get(category.lower(), self.config.category_weights.get("unknown", 0.80))

    def _flow_score(self, market: Market) -> float:
        thresholds = self.config.flow_risk_thresholds
        base = market.flow_stability_score(
            max_volume_change_1h_pct=thresholds.max_volume_change_1h_pct,
            max_abs_one_hour_price_change=thresholds.max_abs_one_hour_price_change,
            min_liquidity=thresholds.min_liquidity,
        )
        penalty = 1.0
        if market.volume_change_1h_pct is not None:
            volume_ratio = abs(market.volume_change_1h_pct) / max(thresholds.max_volume_change_1h_pct, 1e-9)
            if volume_ratio >= 1.0:
                penalty *= 0.0
            elif volume_ratio >= 0.95:
                penalty *= 0.35
            elif volume_ratio >= 0.85:
                penalty *= 0.65
        if market.one_hour_price_change is not None:
            price_ratio = abs(market.one_hour_price_change) / max(thresholds.max_abs_one_hour_price_change, 1e-9)
            if price_ratio >= 1.0:
                penalty *= 0.0
            elif price_ratio >= 0.95:
                penalty *= 0.35
            elif price_ratio >= 0.85:
                penalty *= 0.65
        if market.liquidity is not None and market.liquidity < thresholds.min_liquidity * 1.5:
            penalty *= 0.75
        return round(min(max(base * penalty, 0.0), 1.5), 4)

    def _momentum_volume_score(self, market: Market, *, spot_snapshot: SpotSnapshot | None = None) -> float:
        min_volume_change = self.config.momentum_min_abs_volume_change_1h_pct or 15.0
        min_price_change = self.config.momentum_min_abs_one_hour_price_change or 0.015
        volume_ratio = abs(market.volume_change_1h_pct or 0.0) / max(min_volume_change, 1e-9)
        price_ratio = abs(market.one_hour_price_change or 0.0) / max(min_price_change, 1e-9)
        spot_ratio = 0.0
        if spot_snapshot is not None:
            if self.config.spot_min_abs_return_1h_pct:
                spot_ratio = max(spot_ratio, abs(spot_snapshot.return_1h_pct or 0.0) / max(self.config.spot_min_abs_return_1h_pct, 1e-9))
            if self.config.spot_min_abs_return_15m_pct:
                spot_ratio = max(spot_ratio, abs(spot_snapshot.return_15m_pct or 0.0) / max(self.config.spot_min_abs_return_15m_pct, 1e-9))
        signal_ratio = max(volume_ratio, price_ratio, spot_ratio)
        if signal_ratio >= 4.0:
            return 1.5
        if signal_ratio >= 3.0:
            return 1.3
        if signal_ratio >= 2.0:
            return 1.1
        if signal_ratio >= 1.0:
            return 0.9
        return 0.35

    def _momentum_flow_score(self, market: Market, *, spot_snapshot: SpotSnapshot | None = None) -> float:
        min_volume_change = self.config.momentum_min_abs_volume_change_1h_pct or 15.0
        min_price_change = self.config.momentum_min_abs_one_hour_price_change or 0.015
        volume_ratio = abs(market.volume_change_1h_pct or 0.0) / max(min_volume_change, 1e-9)
        price_ratio = abs(market.one_hour_price_change or 0.0) / max(min_price_change, 1e-9)
        signal_ratio = max(volume_ratio, price_ratio)
        if spot_snapshot is not None:
            if self.config.spot_max_age_seconds is not None and spot_snapshot.age_seconds > self.config.spot_max_age_seconds:
                return 0.0
            if self.config.spot_min_abs_return_1h_pct is not None:
                signal_ratio = max(signal_ratio, abs(spot_snapshot.return_1h_pct or 0.0) / max(self.config.spot_min_abs_return_1h_pct, 1e-9))
            if self.config.spot_min_abs_return_15m_pct is not None:
                signal_ratio = max(signal_ratio, abs(spot_snapshot.return_15m_pct or 0.0) / max(self.config.spot_min_abs_return_15m_pct, 1e-9))
        if market.liquidity is not None and market.liquidity >= self.config.min_liquidity * 5:
            liquidity_bonus = 0.2
        elif market.liquidity is not None and market.liquidity < self.config.min_liquidity:
            liquidity_bonus = -0.35
        else:
            liquidity_bonus = 0.0
        score = 0.5 + min(signal_ratio, 4.0) * 0.25 + liquidity_bonus
        if spot_snapshot is not None and self.config.spot_min_contract_lag_pct is not None:
            lag = abs(spot_snapshot.return_1h_pct or 0.0) - abs(market.one_hour_price_change or 0.0)
            if lag >= self.config.spot_min_contract_lag_pct:
                score += 0.25
            elif lag <= 0:
                score -= 0.35
        return round(min(max(score, 0.0), 1.5), 4)

    def _momentum_catalyst_score(
        self,
        catalyst_snapshot: CatalystSnapshot | None,
        *,
        as_of: datetime,
    ) -> float:
        if catalyst_snapshot is None or not catalyst_snapshot.active_events:
            return 0.0
        best_score = 0.0
        for event in catalyst_snapshot.active_events:
            minutes_to_event = abs(event.minutes_until(as_of))
            freshness = max(0.0, 1.0 - min(minutes_to_event, 90.0) / 90.0)
            importance = max(1.0, min(float(event.importance), 3.0)) / 3.0
            score = 0.4 + (freshness * 0.8) + (importance * 0.4)
            if event.matches_any_keyword(self.config.catalyst_event_keywords):
                score += 0.25
            best_score = max(best_score, score)
        return round(min(best_score, 1.5), 4)

    def _volume_score(self, volume_change_1h_pct: float | None) -> float:
        if volume_change_1h_pct is None:
            return 1.0
        magnitude = abs(volume_change_1h_pct)
        if magnitude <= 5:
            return 1.5
        if magnitude <= 15:
            return 1.0
        return 0.25

    def _spread_score(self) -> float:
        # Spread is no longer a gating signal in the current strategy,
        # so keep it neutral rather than suppressing the ranked pool.
        return 1.0
