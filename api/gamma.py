from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import Any

import aiohttp

from models.market import Market

logger = logging.getLogger(__name__)


class GammaAPIError(RuntimeError):
    """Raised when the Gamma API returns an invalid response."""


class GammaClient:
    def __init__(
        self,
        base_url: str = "https://gamma-api.polymarket.com",
        *,
        session: aiohttp.ClientSession | Any | None = None,
        timeout_seconds: float = 10.0,
        page_delay_seconds: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._request_timeout_seconds = float(timeout_seconds)
        self._page_delay_seconds = max(0.0, float(page_delay_seconds))
        self._timeout = aiohttp.ClientTimeout(
            total=self._request_timeout_seconds,
            connect=min(5.0, self._request_timeout_seconds),
            sock_connect=min(5.0, self._request_timeout_seconds),
            sock_read=self._request_timeout_seconds,
        )

    async def __aenter__(self) -> "GammaClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

    async def fetch_markets(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        archived: bool | None = False,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Market]:
        payload = await self._get_json(
            "/markets",
            params={
                "active": _as_query_flag(active),
                "closed": _as_query_flag(closed),
                "archived": _as_query_flag(archived),
                "limit": limit,
                "offset": offset,
            },
        )
        if not isinstance(payload, list):
            raise GammaAPIError("Expected /markets response to be a list")
        markets: list[Market] = []
        for item in payload:
            try:
                markets.append(Market.from_gamma_market(item))
            except ValueError:
                continue
        return markets

    async def fetch_market(self, market_id: str) -> Market:
        payload = await self._get_json(f"/markets/{market_id}")
        if not isinstance(payload, dict):
            raise GammaAPIError("Expected /markets/{id} response to be an object")
        return Market.from_gamma_market(payload)

    async def fetch_market_by_slug(self, slug: str) -> Market:
        payload = await self._get_json("/markets", params={"slug": slug})
        if not isinstance(payload, list) or not payload:
            raise GammaAPIError(f"No market found for slug: {slug}")
        return Market.from_gamma_market(payload[0])

    async def fetch_all_open_markets(self, *, page_size: int = 500) -> list[Market]:
        if page_size <= 0:
            raise ValueError("page_size must be positive")

        all_markets: list[Market] = []
        seen_market_ids: set[str] = set()
        offset = 0

        while True:
            page_started = perf_counter()
            logger.debug(
                "Gamma fetch_all_open_markets page start offset=%s limit=%s timeout=%.1fs",
                offset,
                page_size,
                self._request_timeout_seconds,
            )
            payload = await self._get_json(
                "/markets",
                params={
                    "active": _as_query_flag(True),
                    "closed": _as_query_flag(False),
                    "archived": _as_query_flag(False),
                    "limit": page_size,
                    "offset": offset,
                },
            )
            if not isinstance(payload, list):
                raise GammaAPIError("Expected /markets response to be a list")
            parsed_batch: list[Market] = []
            for item in payload:
                try:
                    market = Market.from_gamma_market(item)
                except ValueError:
                    continue
                if market.market_id in seen_market_ids:
                    continue
                seen_market_ids.add(market.market_id)
                parsed_batch.append(market)
            all_markets.extend(parsed_batch)
            logger.debug(
                "Gamma fetch_all_open_markets page end offset=%s fetched=%s parsed=%s elapsed=%.3fs",
                offset,
                len(payload),
                len(parsed_batch),
                perf_counter() - page_started,
            )
            if len(payload) < page_size:
                break
            if not parsed_batch:
                break
            offset += page_size
            if self._page_delay_seconds > 0:
                await asyncio.sleep(self._page_delay_seconds)

        return all_markets

    async def _get_json(
        self,
        path: str,
        *,
        params: dict[str, object | None] | None = None,
    ) -> object:
        session = await self._ensure_session()
        cleaned_params = {
            key: value for key, value in (params or {}).items() if value is not None
        }
        url = f"{self.base_url}{path}"
        started = perf_counter()
        logger.debug("Gamma GET start path=%s params=%s timeout=%.1fs", path, cleaned_params, self._request_timeout_seconds)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                async with session.get(url, params=cleaned_params) as response:
                    body: str | None = None
                    if response.status >= 400:
                        body = await response.text()
                        raise GammaAPIError(
                            f"Gamma API request failed ({response.status}) path={path} params={cleaned_params}: {body[:200]}"
                        )
                    payload = await response.json()
                    logger.debug(
                        "Gamma GET end path=%s status=%s elapsed=%.3fs",
                        path,
                        response.status,
                        perf_counter() - started,
                    )
                    return payload
        except TimeoutError as exc:
            logger.warning(
                "Gamma GET timeout path=%s params=%s timeout=%.1fs elapsed=%.3fs",
                path,
                cleaned_params,
                self._request_timeout_seconds,
                perf_counter() - started,
            )
            raise GammaAPIError(
                f"Gamma API request timed out after {self._request_timeout_seconds:.1f}s: {path}"
            ) from exc

    async def _ensure_session(self) -> aiohttp.ClientSession | Any:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session


def _as_query_flag(value: bool | None) -> str | None:
    if value is None:
        return None
    return "true" if value else "false"
