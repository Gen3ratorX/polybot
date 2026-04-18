from __future__ import annotations

import asyncio
from collections.abc import Mapping

from api.spot import SpotFeedClient, SpotSnapshot
from bot.config import EnvironmentConfig, RuntimeConfig
from bot.runtime_state import send_optional_alert
from bot.tracker import TradeTracker
from models import Market


async def load_active_spot_snapshot(
    *,
    env: EnvironmentConfig | None,
    runtime: RuntimeConfig,
    tracker: TradeTracker | None = None,
) -> SpotSnapshot | None:
    snapshots = await load_active_spot_snapshots(env=env, runtime=runtime, tracker=tracker)
    if not snapshots:
        return None
    return next(iter(snapshots.values()))


async def load_active_spot_snapshots(
    *,
    env: EnvironmentConfig | None,
    runtime: RuntimeConfig,
    tracker: TradeTracker | None = None,
) -> dict[str, SpotSnapshot]:
    if runtime.strategy.signal_mode != "momentum":
        return {}

    symbols = runtime.strategy.spot_symbols or (runtime.strategy.spot_symbol,)
    async with SpotFeedClient() as spot_client:
        async def _load_snapshot(symbol: str) -> tuple[str, SpotSnapshot | None]:
            key = symbol.strip().lower()
            cached = tracker.get_latest_spot_snapshot(symbol) if tracker is not None else None
            try:
                snapshot = await spot_client.fetch_snapshot(symbol)
            except Exception as exc:
                if cached is not None:
                    return key, cached
                if env is not None:
                    send_optional_alert(
                        env,
                        f"Spot feed unavailable for {symbol}: {exc}",
                        level="WARN",
                    )
                return key, None
            if tracker is not None:
                tracker.record_spot_tick(snapshot)
            return key, snapshot

        results = await asyncio.gather(*(_load_snapshot(symbol) for symbol in symbols))
        snapshots: dict[str, SpotSnapshot] = {
            key: snapshot for key, snapshot in results if snapshot is not None
        }
    return snapshots


def resolve_spot_snapshot_for_market(
    market: Market,
    spot_snapshot: SpotSnapshot | Mapping[str, SpotSnapshot] | None,
    *,
    available_symbols: tuple[str, ...] = (),
) -> SpotSnapshot | None:
    if spot_snapshot is None:
        return None
    if isinstance(spot_snapshot, Mapping):
        symbol = infer_spot_symbol_for_market(market, available_symbols or tuple(spot_snapshot.keys()))
        if symbol is None:
            return None
        resolved = spot_snapshot.get(symbol)
        if resolved is not None:
            return resolved
        resolved = spot_snapshot.get(symbol.lower())
        if resolved is not None:
            return resolved
        resolved = spot_snapshot.get(symbol.upper())
        if resolved is not None:
            return resolved
        return None
    return spot_snapshot


def infer_spot_symbol_for_market(market: Market, available_symbols: tuple[str, ...]) -> str | None:
    if not available_symbols:
        return None
    corpus = market.strategy_text_corpus()
    normalized = tuple(symbol.strip().lower() for symbol in available_symbols)
    preferred = [
        ("ethereum", "eth", "ethusd"),
        ("solana", "sol", "solusd"),
        ("bitcoin", "btc", "xbtusd"),
    ]
    for _, keyword, symbol in preferred:
        if symbol not in normalized:
            continue
        if keyword == "eth":
            if " ethereum " in f" {corpus} " or " eth " in f" {corpus} ":
                return symbol
        elif keyword == "sol":
            if " solana " in f" {corpus} " or " sol " in f" {corpus} ":
                return symbol
        elif keyword == "btc":
            if " bitcoin " in f" {corpus} " or " btc " in f" {corpus} ":
                return symbol
    for symbol in normalized:
        if symbol in {"xbtusd", "btcusd"}:
            return symbol
    return normalized[0]
