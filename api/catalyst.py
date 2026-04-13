from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any
from urllib.parse import quote

import aiohttp


class CatalystFeedError(RuntimeError):
    """Raised when the external catalyst feed returns an invalid response."""


@dataclass(frozen=True, slots=True)
class CatalystEvent:
    calendar_id: str
    country: str
    category: str
    event: str
    date: datetime
    importance: int
    source: str | None = None
    source_url: str | None = None
    url: str | None = None
    reference: str | None = None
    reference_date: datetime | None = None
    actual: str | None = None
    previous: str | None = None
    forecast: str | None = None
    te_forecast: str | None = None
    ticker: str | None = None
    symbol: str | None = None
    currency: str | None = None
    unit: str | None = None
    last_update: datetime | None = None
    payload: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["date"] = self.date.astimezone(UTC).isoformat()
        if self.reference_date is not None:
            payload["reference_date"] = self.reference_date.astimezone(UTC).isoformat()
        if self.last_update is not None:
            payload["last_update"] = self.last_update.astimezone(UTC).isoformat()
        return payload

    def matches_any_keyword(self, keywords: tuple[str, ...] | list[str]) -> bool:
        if not keywords:
            return True
        haystack = " ".join(
            part
            for part in (
                self.country,
                self.category,
                self.event,
                self.ticker,
                self.symbol,
                self.currency,
                self.unit,
            )
            if part
        ).lower()
        for keyword in keywords:
            normalized = str(keyword).strip().lower()
            if normalized and normalized in haystack:
                return True
        return False

    def is_active(
        self,
        *,
        as_of: datetime,
        arm_before_minutes: int,
        arm_after_minutes: int,
    ) -> bool:
        reference = as_of.astimezone(UTC)
        start = self.date.astimezone(UTC) - timedelta(minutes=arm_before_minutes)
        end = self.date.astimezone(UTC) + timedelta(minutes=arm_after_minutes)
        return start <= reference <= end

    def minutes_until(self, as_of: datetime) -> float:
        reference = as_of.astimezone(UTC)
        return (self.date.astimezone(UTC) - reference).total_seconds() / 60


@dataclass(frozen=True, slots=True)
class CatalystSnapshot:
    provider: str
    observed_at: datetime
    fetch_latency_seconds: float | None
    events: tuple[CatalystEvent, ...]
    active_events: tuple[CatalystEvent, ...]
    payload: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["observed_at"] = self.observed_at.astimezone(UTC).isoformat()
        return payload

    @property
    def active(self) -> bool:
        return bool(self.active_events)


class TradingEconomicsCalendarClient:
    def __init__(
        self,
        base_url: str = "https://api.tradingeconomics.com",
        *,
        credentials: str | None = None,
        session: aiohttp.ClientSession | Any | None = None,
        timeout_seconds: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials.strip() if credentials else None
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def __aenter__(self) -> "TradingEconomicsCalendarClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

    async def fetch_events(
        self,
        *,
        countries: tuple[str, ...] | list[str],
        start_date: datetime,
        end_date: datetime,
    ) -> tuple[CatalystEvent, ...]:
        normalized_countries = tuple(
            country.strip().lower()
            for country in countries
            if str(country).strip()
        )
        if not normalized_countries:
            normalized_countries = ("united states",)

        events: list[CatalystEvent] = []
        for country in normalized_countries:
            events.extend(
                await self._fetch_country_events(
                    country,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
        events.sort(key=lambda item: (item.date, item.country, item.event))
        return tuple(events)

    async def _fetch_country_events(
        self,
        country: str,
        *,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CatalystEvent]:
        encoded_country = quote(country.strip())
        path = f"/calendar/country/{encoded_country}/{_format_date(start_date)}/{_format_date(end_date)}"
        payload = await self._get_json(path, params={"f": "json"})
        if not isinstance(payload, list):
            raise CatalystFeedError("Trading Economics calendar payload must be a list")
        return [_parse_event(item) for item in payload if isinstance(item, dict)]

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
    ) -> object:
        session = await self._ensure_session()
        cleaned_params = {key: value for key, value in (params or {}).items() if value is not None}
        cleaned_params["c"] = self.credentials or "guest:guest"
        async with session.get(f"{self.base_url}{path}", params=cleaned_params) as response:
            if response.status >= 400:
                body = await response.text()
                raise CatalystFeedError(f"Catalyst feed request failed ({response.status}): {body[:200]}")
            return await response.json()

    async def _ensure_session(self) -> aiohttp.ClientSession | Any:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session


def _format_date(value: datetime) -> str:
    return value.astimezone(UTC).date().isoformat()


def _parse_event(payload: dict[str, object]) -> CatalystEvent:
    calendar_id = _as_text(payload.get("CalendarId"), "CalendarId")
    date = _parse_datetime(payload.get("Date"), field_name="Date")
    reference_date = _parse_datetime(payload.get("ReferenceDate"), field_name="ReferenceDate")
    last_update = _parse_datetime(payload.get("LastUpdate"), field_name="LastUpdate")
    importance = _as_int(payload.get("Importance"), "Importance")
    return CatalystEvent(
        calendar_id=calendar_id,
        country=_optional_text(payload.get("Country")) or "unknown",
        category=_optional_text(payload.get("Category")) or "unknown",
        event=_optional_text(payload.get("Event")) or "unknown",
        date=date,
        importance=importance,
        source=_optional_text(payload.get("Source")),
        source_url=_optional_text(payload.get("SourceURL")),
        url=_optional_text(payload.get("URL")),
        reference=_optional_text(payload.get("Reference")),
        reference_date=reference_date,
        actual=_optional_text(payload.get("Actual")),
        previous=_optional_text(payload.get("Previous")),
        forecast=_optional_text(payload.get("Forecast")),
        te_forecast=_optional_text(payload.get("TEForecast")),
        ticker=_optional_text(payload.get("Ticker")),
        symbol=_optional_text(payload.get("Symbol")),
        currency=_optional_text(payload.get("Currency")),
        unit=_optional_text(payload.get("Unit")),
        last_update=last_update,
        payload=payload,
    )


def _as_text(value: object | None, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalystFeedError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    text = value.strip()
    return text or None


def _as_int(value: object | None, field_name: str) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CatalystFeedError(f"{field_name} must be numeric") from exc


def _parse_datetime(value: object | None, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if not isinstance(value, str) or not value.strip():
        raise CatalystFeedError(f"{field_name} must be a datetime string")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CatalystFeedError(f"{field_name} is not a valid datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
