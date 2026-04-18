from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from api.catalyst import CatalystSnapshot
from api.gamma import GammaClient
from api.spot import SpotSnapshot
from bot.config import StrategyProfile
from bot.spot import resolve_spot_snapshot_for_market
from models.market import Market

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScanDecision:
    market: Market
    qualifies: bool
    hours_to_close: float
    reasons: tuple[str, ...]


class MarketScanner:
    def __init__(self, gamma_client: GammaClient, config: StrategyProfile) -> None:
        self.gamma_client = gamma_client
        self.config = config

    async def scan(
        self,
        *,
        as_of: datetime | None = None,
        spot_snapshot: SpotSnapshot | dict[str, SpotSnapshot] | None = None,
        catalyst_snapshot: CatalystSnapshot | None = None,
    ) -> list[Market]:
        markets = await self.load_markets()
        return self.filter_markets(markets, as_of=as_of, spot_snapshot=spot_snapshot, catalyst_snapshot=catalyst_snapshot)

    async def load_markets(self) -> list[Market]:
        max_pages = self._gamma_max_pages()
        if self.config.signal_mode != "momentum":
            return await self.gamma_client.fetch_all_open_markets(
                max_pages=max_pages,
                order="volume_24hr",
                ascending=False,
            )

        tag_ids = await self._resolve_gamma_tag_ids()
        if not tag_ids:
            return await self.gamma_client.fetch_all_open_markets(
                max_pages=max_pages,
                order="volume_24hr",
                ascending=False,
            )

        markets_by_id: dict[str, Market] = {}
        for tag_id in tag_ids:
            tagged_markets = await self.gamma_client.fetch_all_open_markets(
                max_pages=max_pages,
                tag_id=tag_id,
                order="volume_24hr",
                ascending=False,
            )
            for market in tagged_markets:
                markets_by_id.setdefault(market.market_id, market)
        logger.debug(
            "Loaded %s markets across %s tag filters for profile=%s",
            len(markets_by_id),
            len(tag_ids),
            self.config.name,
        )
        return list(markets_by_id.values())

    def filter_markets(
        self,
        markets: list[Market],
        *,
        as_of: datetime | None = None,
        spot_snapshot: SpotSnapshot | dict[str, SpotSnapshot] | None = None,
        catalyst_snapshot: CatalystSnapshot | None = None,
    ) -> list[Market]:
        reference = as_of or datetime.now(UTC)
        return [
            decision.market
            for decision in self.diagnose_markets(
                markets,
                as_of=reference,
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
            )
            if decision.qualifies
        ]

    def qualifies(
        self,
        market: Market,
        *,
        as_of: datetime | None = None,
        spot_snapshot: SpotSnapshot | dict[str, SpotSnapshot] | None = None,
        catalyst_snapshot: CatalystSnapshot | None = None,
    ) -> bool:
        return self.diagnose_market(
            market,
            as_of=as_of,
            spot_snapshot=spot_snapshot,
            catalyst_snapshot=catalyst_snapshot,
        ).qualifies

    def diagnose_markets(
        self,
        markets: list[Market],
        *,
        as_of: datetime | None = None,
        spot_snapshot: SpotSnapshot | dict[str, SpotSnapshot] | None = None,
        catalyst_snapshot: CatalystSnapshot | None = None,
    ) -> list[ScanDecision]:
        reference = as_of or datetime.now(UTC)
        return [
            self.diagnose_market(
                market,
                as_of=reference,
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
            )
            for market in markets
        ]

    def diagnose_market(
        self,
        market: Market,
        *,
        as_of: datetime | None = None,
        spot_snapshot: SpotSnapshot | dict[str, SpotSnapshot] | None = None,
        catalyst_snapshot: CatalystSnapshot | None = None,
    ) -> ScanDecision:
        reference = as_of or datetime.now(UTC)
        hours_to_close = market.hours_to_close(reference)
        reasons: list[str] = []

        if not market.active or market.closed or market.archived:
            reasons.append("status")
        if hours_to_close < 0:
            reasons.append("time_past_close")
        elif hours_to_close < self.config.min_hours_to_close:
            reasons.append("time_too_early")
        if hours_to_close > self.config.max_hours_to_close:
            reasons.append("time_too_late")
        if market.volume < self.config.min_volume:
            reasons.append("volume")
        if self.config.signal_mode == "momentum":
            self._apply_momentum_filters(
                market,
                reasons,
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
                as_of=reference,
            )
        else:
            qualitative = False
            if self.config.require_deterministic_markets and not market.is_deterministic_candidate():
                qualitative = True
            if market.qualitative_risk_signals():
                qualitative = True
            if qualitative:
                reasons.append("qualitative")
            reasons.extend(
                market.flow_shock_reasons(
                    max_volume_change_1h_pct=self.config.max_volume_change_1h_pct,
                    max_abs_one_hour_price_change=self.config.max_abs_one_hour_price_change,
                    min_liquidity=self.config.min_liquidity,
                )
            )
        if not self._has_qualifying_price(market):
            reasons.append("price")

        return ScanDecision(
            market=market,
            qualifies=not reasons,
            hours_to_close=hours_to_close,
            reasons=tuple(reasons),
        )

    def _has_qualifying_price(self, market: Market) -> bool:
        max_price = self._max_price_for_market(market)
        return any(
            self.config.min_price <= price <= max_price
            for price in (market.yes_price, market.no_price)
        )

    def _max_price_for_market(self, market: Market) -> float:
        if market.category.lower() == "sports":
            return self.config.sports_max_price
        return self.config.max_price

    def _apply_momentum_filters(
        self,
        market: Market,
        reasons: list[str],
        *,
        spot_snapshot: SpotSnapshot | dict[str, SpotSnapshot] | None,
        catalyst_snapshot: CatalystSnapshot | None,
        as_of: datetime | None,
    ) -> None:
        if market.category.lower() not in {"crypto", "unknown"}:
            reasons.append("category")
        if self.config.require_deterministic_markets and not market.is_deterministic_candidate():
            reasons.append("deterministic")
        if self.config.asset_keywords and not market.matches_any_keyword(self.config.asset_keywords):
            reasons.append("asset_keyword")
        resolved_spot_snapshot = resolve_spot_snapshot_for_market(
            market,
            spot_snapshot,
            available_symbols=self.config.spot_symbols,
        )
        catalyst_mode = self.config.catalyst_mode
        catalyst_soft_boost = catalyst_mode in {"soft", "soft_boost", "boost"}
        if not catalyst_soft_boost:
            if self.config.catalyst_keywords and not market.matches_any_keyword(self.config.catalyst_keywords):
                reasons.append("catalyst")
            if self.config.catalyst_provider == "trading_economics":
                if catalyst_snapshot is None or not catalyst_snapshot.active_events:
                    reasons.append("catalyst_inactive")
                else:
                    active_events = catalyst_snapshot.active_events
                    if self.config.catalyst_min_importance is not None and not any(
                        event.importance >= self.config.catalyst_min_importance for event in active_events
                    ):
                        reasons.append("catalyst_importance")
                    if self.config.catalyst_event_keywords and not any(
                        event.matches_any_keyword(self.config.catalyst_event_keywords) for event in active_events
                    ):
                        reasons.append("catalyst_event")
            elif self.config.catalyst_time_windows_utc and not _within_utc_time_windows(
                as_of or datetime.now(UTC),
                self.config.catalyst_time_windows_utc,
            ):
                reasons.append("catalyst_window")
        if market.qualitative_risk_signals():
            reasons.append("qualitative")
        if market.liquidity is not None and market.liquidity < self.config.min_liquidity:
            reasons.append("thin_liquidity")

        if self.config.spot_symbols and resolved_spot_snapshot is None:
            reasons.append("spot_missing")
        if resolved_spot_snapshot is not None:
            max_age = self.config.spot_max_age_seconds
            if max_age is not None and resolved_spot_snapshot.age_seconds > max_age:
                reasons.append("spot_stale")
            min_return_1h = self.config.spot_min_abs_return_1h_pct
            if min_return_1h is not None:
                if resolved_spot_snapshot.return_1h_pct is None or abs(resolved_spot_snapshot.return_1h_pct) < min_return_1h:
                    reasons.append("spot_return_1h")
            min_return_15m = self.config.spot_min_abs_return_15m_pct
            if min_return_15m is not None:
                if resolved_spot_snapshot.return_15m_pct is None or abs(resolved_spot_snapshot.return_15m_pct) < min_return_15m:
                    reasons.append("spot_return_15m")
            min_contract_lag = self.config.spot_min_contract_lag_pct
            if min_contract_lag is not None:
                spot_move = abs(resolved_spot_snapshot.return_1h_pct or 0.0)
                contract_move = abs(market.one_hour_price_change or 0.0)
                if market.one_hour_price_change is None or (spot_move - contract_move) < min_contract_lag:
                    reasons.append("spot_lag")

        min_volume_move = self.config.momentum_min_abs_volume_change_1h_pct
        if min_volume_move is not None:
            if market.volume_change_1h_pct is None or abs(market.volume_change_1h_pct) < min_volume_move:
                reasons.append("momentum_volume")

        min_price_move = self.config.momentum_min_abs_one_hour_price_change
        if min_price_move is not None:
            if market.one_hour_price_change is None or abs(market.one_hour_price_change) < min_price_move:
                reasons.append("momentum_price")

    def _gamma_max_pages(self) -> int:
        configured = self.config.gamma_max_pages
        if configured is not None and configured > 0:
            return configured
        return 10 if self.config.signal_mode == "momentum" else 20

    async def _resolve_gamma_tag_ids(self) -> tuple[str, ...]:
        slugs = tuple(slug.strip().lower() for slug in self.config.gamma_tag_slugs if slug.strip())
        if not slugs:
            return ()
        resolver = getattr(self.gamma_client, "fetch_tag_by_slug", None)
        if resolver is None:
            logger.debug(
                "Gamma client does not expose fetch_tag_by_slug; skipping tag filters for profile=%s",
                self.config.name,
            )
            return ()
        resolved: list[str] = []
        for slug in slugs:
            payload = await resolver(slug)
            tag_id = payload.get("id") if isinstance(payload, dict) else None
            if tag_id is None:
                logger.debug("Gamma tag slug %s returned no id; skipping", slug)
                continue
            resolved.append(str(tag_id))
        return tuple(resolved)


def _within_utc_time_windows(reference: datetime, windows: tuple[str, ...]) -> bool:
    current_minutes = reference.astimezone(UTC).hour * 60 + reference.astimezone(UTC).minute
    for window in windows:
        parsed = _parse_time_window(window)
        if parsed is None:
            continue
        start_minutes, end_minutes = parsed
        if start_minutes <= end_minutes:
            if start_minutes <= current_minutes <= end_minutes:
                return True
        else:
            if current_minutes >= start_minutes or current_minutes <= end_minutes:
                return True
    return False


def _parse_time_window(value: str) -> tuple[int, int] | None:
    text = value.strip()
    if not text or "-" not in text:
        return None
    start_text, end_text = text.split("-", 1)
    try:
        start_minutes = _parse_clock_minutes(start_text)
        end_minutes = _parse_clock_minutes(end_text)
    except ValueError:
        return None
    return start_minutes, end_minutes


def _parse_clock_minutes(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError("Expected HH:MM time window")
    hour = int(parts[0])
    minute = int(parts[1])
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError("Expected HH:MM time window")
    return hour * 60 + minute
