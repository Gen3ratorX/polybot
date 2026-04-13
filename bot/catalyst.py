from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter

from api.catalyst import CatalystEvent, CatalystSnapshot, TradingEconomicsCalendarClient
from bot.config import EnvironmentConfig, RuntimeConfig
from bot.runtime_state import send_optional_alert
from bot.tracker import TradeTracker


@dataclass(frozen=True, slots=True)
class CatalystContext:
    snapshot: CatalystSnapshot
    active_event: CatalystEvent | None

    def as_dict(self) -> dict[str, object]:
        payload = self.snapshot.as_dict()
        payload["active_event"] = None if self.active_event is None else self.active_event.as_dict()
        return payload


async def load_active_catalyst_snapshot(
    *,
    env: EnvironmentConfig | None,
    runtime: RuntimeConfig,
    tracker: TradeTracker | None = None,
    as_of: datetime | None = None,
) -> CatalystSnapshot | None:
    if runtime.strategy.signal_mode != "momentum":
        return None
    observed_at = as_of or datetime.now(UTC)
    if runtime.strategy.catalyst_provider != "trading_economics":
        return _cached_catalyst_snapshot(tracker, runtime, observed_at=observed_at)

    started = perf_counter()
    try:
        async with TradingEconomicsCalendarClient(credentials=_catalyst_credentials(env)) as client:
            events = await client.fetch_events(
                countries=runtime.strategy.catalyst_countries or ("united states",),
                start_date=observed_at - timedelta(minutes=runtime.strategy.catalyst_arm_after_minutes),
                end_date=observed_at + timedelta(days=runtime.strategy.catalyst_lookahead_days),
            )
    except Exception as exc:
        if env is not None:
            send_optional_alert(
                env,
                f"Catalyst feed unavailable for {runtime.strategy.name}: {exc}",
                level="WARN",
            )
        return None

    if tracker is not None and events:
        tracker.record_catalyst_events(events, provider=runtime.strategy.catalyst_provider)
    return _build_snapshot(
        provider=runtime.strategy.catalyst_provider,
        events=events,
        runtime=runtime,
        observed_at=observed_at,
        fetch_latency_seconds=round(perf_counter() - started, 6),
    )


def load_cached_catalyst_snapshot(
    *,
    tracker: TradeTracker,
    runtime: RuntimeConfig,
    as_of: datetime | None = None,
) -> CatalystSnapshot | None:
    if runtime.strategy.signal_mode != "momentum":
        return None
    observed_at = as_of or datetime.now(UTC)
    return _cached_catalyst_snapshot(tracker, runtime, observed_at=observed_at)


def build_catalyst_context(
    *,
    tracker: TradeTracker,
    runtime: RuntimeConfig,
    as_of: datetime | None = None,
) -> CatalystContext | None:
    snapshot = load_cached_catalyst_snapshot(tracker=tracker, runtime=runtime, as_of=as_of)
    if snapshot is None:
        return None
    active_event = snapshot.active_events[0] if snapshot.active_events else None
    return CatalystContext(snapshot=snapshot, active_event=active_event)


def _cached_catalyst_snapshot(
    tracker: TradeTracker | None,
    runtime: RuntimeConfig,
    *,
    observed_at: datetime,
) -> CatalystSnapshot | None:
    if tracker is None:
        return None
    events = tracker.list_recent_catalyst_events(
        provider=runtime.strategy.catalyst_provider,
        limit=max(50, runtime.strategy.catalyst_lookahead_days * 20),
    )
    if not events:
        return None
    return _build_snapshot(
        provider=runtime.strategy.catalyst_provider,
        events=events,
        runtime=runtime,
        observed_at=observed_at,
        fetch_latency_seconds=None,
    )


def _build_snapshot(
    *,
    provider: str,
    events: tuple[CatalystEvent, ...],
    runtime: RuntimeConfig,
    observed_at: datetime,
    fetch_latency_seconds: float | None,
) -> CatalystSnapshot:
    active_events = tuple(
        event
        for event in events
        if event.is_active(
            as_of=observed_at,
            arm_before_minutes=runtime.strategy.catalyst_arm_before_minutes,
            arm_after_minutes=runtime.strategy.catalyst_arm_after_minutes,
        )
        and (
            runtime.strategy.catalyst_min_importance is None
            or event.importance >= runtime.strategy.catalyst_min_importance
        )
        and (
            not runtime.strategy.catalyst_event_keywords
            or event.matches_any_keyword(runtime.strategy.catalyst_event_keywords)
        )
        and (
            not runtime.strategy.catalyst_countries
            or _country_match(event.country, runtime.strategy.catalyst_countries)
        )
    )
    return CatalystSnapshot(
        provider=provider,
        observed_at=observed_at,
        fetch_latency_seconds=fetch_latency_seconds,
        events=events,
        active_events=active_events,
        payload={
            "event_count": len(events),
            "active_event_count": len(active_events),
            "provider": provider,
            "countries": list(runtime.strategy.catalyst_countries),
        },
    )


def _catalyst_credentials(env: EnvironmentConfig | None) -> str | None:
    if env is None:
        return None
    return env.trading_economics_credentials


def _country_match(country: str, countries: tuple[str, ...]) -> bool:
    normalized = country.strip().lower()
    return any(normalized == allowed.strip().lower() for allowed in countries)
