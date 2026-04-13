from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from time import perf_counter

import aiohttp


class SpotFeedError(RuntimeError):
    """Raised when the BTC spot feed returns an invalid response."""


@dataclass(frozen=True, slots=True)
class SpotSnapshot:
    symbol: str
    pair: str
    spot_price: float
    return_15m_pct: float | None
    return_1h_pct: float | None
    fetch_latency_seconds: float | None
    observed_at: datetime
    age_seconds: float
    source: str = "kraken"
    payload: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["observed_at"] = self.observed_at.astimezone(UTC).isoformat()
        return payload


class SpotFeedClient:
    def __init__(
        self,
        base_url: str = "https://api.kraken.com",
        *,
        session: aiohttp.ClientSession | Any | None = None,
        timeout_seconds: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def __aenter__(self) -> "SpotFeedClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

    async def fetch_snapshot(self, symbol: str = "XBTUSD") -> SpotSnapshot:
        started = perf_counter()
        observed_at = datetime.now(UTC)
        normalized_symbol = _normalize_symbol(symbol)
        pair_candidates = _pair_candidates(normalized_symbol)

        ticker_payload, ticker_pair = await self._fetch_public_result("/0/public/Ticker", pair_candidates)
        spot_price = _extract_ticker_price(ticker_payload)

        ohlc_15_payload, ohlc_15_pair = await self._fetch_public_result(
            "/0/public/OHLC",
            pair_candidates,
            extra_params={"interval": 15},
        )
        ohlc_60_payload, ohlc_60_pair = await self._fetch_public_result(
            "/0/public/OHLC",
            pair_candidates,
            extra_params={"interval": 60},
        )

        payload = {
            "ticker": ticker_payload,
            "ohlc_15m": ohlc_15_payload,
            "ohlc_1h": ohlc_60_payload,
        }
        return SpotSnapshot(
            symbol=normalized_symbol,
            pair=ticker_pair or ohlc_60_pair or ohlc_15_pair or normalized_symbol,
            spot_price=spot_price,
            return_15m_pct=_compute_return_pct(ohlc_15_payload),
            return_1h_pct=_compute_return_pct(ohlc_60_payload),
            fetch_latency_seconds=round(perf_counter() - started, 6),
            observed_at=observed_at,
            age_seconds=0.0,
            source="kraken",
            payload=payload,
        )

    async def _fetch_public_result(
        self,
        path: str,
        pair_candidates: tuple[str, ...],
        *,
        extra_params: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], str]:
        last_error: str | None = None
        for pair in pair_candidates:
            params = {"pair": pair}
            if extra_params:
                params.update(extra_params)
            payload = await self._get_json(path, params=params)
            if not isinstance(payload, dict):
                last_error = f"unexpected payload type for {path}"
                continue
            errors = payload.get("error")
            if isinstance(errors, list) and errors:
                last_error = "; ".join(str(item) for item in errors)
                continue
            result = payload.get("result")
            if not isinstance(result, dict) or not result:
                last_error = f"missing result for {path}"
                continue
            result_keys = [key for key in result.keys() if key != "last"]
            if not result_keys:
                last_error = f"missing pair result for {path}"
                continue
            pair_key = result_keys[0]
            pair_payload = result[pair_key]
            if not isinstance(pair_payload, dict) and not isinstance(pair_payload, list):
                last_error = f"unexpected pair payload for {path}"
                continue
            if isinstance(pair_payload, dict):
                # Ticker responses are dicts; OHLC responses are lists.
                return pair_payload, pair_key
            return {"rows": pair_payload, "last": result.get("last")}, pair_key
        raise SpotFeedError(last_error or f"Unable to resolve spot pair for {path}")

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, object] | None = None,
    ) -> object:
        session = await self._ensure_session()
        cleaned_params = {key: value for key, value in (params or {}).items() if value is not None}
        async with session.get(f"{self.base_url}{path}", params=cleaned_params) as response:
            if response.status >= 400:
                body = await response.text()
                raise SpotFeedError(f"Spot API request failed ({response.status}): {body[:200]}")
            return await response.json()

    async def _ensure_session(self) -> aiohttp.ClientSession | Any:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session


def _normalize_symbol(symbol: str) -> str:
    text = symbol.strip().upper().replace(" ", "")
    if not text:
        return "XBTUSD"
    return text


def _pair_candidates(symbol: str) -> tuple[str, ...]:
    candidates: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip().upper()
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    add(symbol)
    if "/" not in symbol:
        if len(symbol) == 6:
            add(f"{symbol[:3]}/{symbol[3:]}")
        if symbol in {"XBTUSD", "BTCUSD"}:
            add("XXBTZUSD")
            add("XBT/USD")
            add("BTC/USD")
    else:
        add(symbol.replace("/", ""))
    return tuple(candidates)


def _extract_ticker_price(payload: dict[str, object]) -> float:
    if "c" in payload and isinstance(payload["c"], list) and payload["c"]:
        return float(payload["c"][0])
    raise SpotFeedError("Ticker payload missing close price")


def _compute_return_pct(payload: dict[str, object]) -> float | None:
    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    previous = _close_from_candle(rows[-2])
    current = _close_from_candle(rows[-1])
    if previous <= 0:
        return None
    return round((current - previous) / previous, 6)


def _close_from_candle(candle: object) -> float:
    if not isinstance(candle, list) or len(candle) < 5:
        raise SpotFeedError("OHLC candle is malformed")
    return float(candle[4])
