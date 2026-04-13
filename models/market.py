from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from models.enums import OutcomeSide


@dataclass(frozen=True, slots=True)
class Market:
    market_id: str
    condition_id: str
    question: str
    end_date: datetime
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    volume: float
    category: str = "unknown"
    event_id: str | None = None
    event_slug: str | None = None
    event_title: str | None = None
    slug: str | None = None
    liquidity: float | None = None
    volume_24hr: float | None = None
    volume_change_1h_pct: float | None = None
    one_hour_price_change: float | None = None
    active: bool = True
    closed: bool = False
    archived: bool = False

    def __post_init__(self) -> None:
        _require_text(self.market_id, "market_id")
        _require_text(self.condition_id, "condition_id")
        _require_text(self.question, "question")
        _require_text(self.yes_token_id, "yes_token_id")
        _require_text(self.no_token_id, "no_token_id")
        _require_aware_datetime(self.end_date, "end_date")

        for name, value in {
            "yes_price": self.yes_price,
            "no_price": self.no_price,
        }.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")

        if self.volume < 0:
            raise ValueError("volume must be non-negative")

        for name, value in {
            "liquidity": self.liquidity,
            "volume_24hr": self.volume_24hr,
        }.items():
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative when provided")

    @property
    def combined_price(self) -> float:
        return round(self.yes_price + self.no_price, 6)

    @property
    def near_certain_side(self) -> OutcomeSide:
        return OutcomeSide.YES if self.yes_price >= self.no_price else OutcomeSide.NO

    @property
    def near_certain_price(self) -> float:
        return max(self.yes_price, self.no_price)

    def hours_to_close(self, as_of: datetime | None = None) -> float:
        reference = as_of or datetime.now(UTC)
        _require_aware_datetime(reference, "as_of")
        delta = self.end_date - reference
        return delta.total_seconds() / 3600

    def token_id_for(self, side: OutcomeSide) -> str:
        return self.yes_token_id if side is OutcomeSide.YES else self.no_token_id

    def resolution_price_for(self, side: OutcomeSide) -> float | None:
        if self.active and not self.closed:
            return None
        yes_resolved = self.yes_price in {0.0, 1.0}
        no_resolved = self.no_price in {0.0, 1.0}
        if not (yes_resolved and no_resolved):
            return None
        return self.yes_price if side is OutcomeSide.YES else self.no_price

    def strategy_text_corpus(self) -> str:
        fields = (
            self.question,
            self.slug,
            self.event_slug,
            self.event_title,
            self.category,
        )
        return " ".join(field for field in fields if field).lower()

    def is_deterministic_candidate(self) -> bool:
        if self.category.lower() in {"sports", "crypto", "politics"}:
            return True
        return _matches_any_pattern(self.strategy_text_corpus(), _DETERMINISTIC_PATTERNS)

    def qualitative_risk_signals(self) -> tuple[str, ...]:
        text = self.strategy_text_corpus()
        signals: list[str] = []
        for label, pattern in _QUALITATIVE_RISK_PATTERNS:
            if re.search(pattern, text):
                signals.append(label)
        return tuple(signals)

    def matches_any_keyword(self, keywords: tuple[str, ...] | list[str]) -> bool:
        text = self.strategy_text_corpus()
        for keyword in keywords:
            normalized = str(keyword).strip().lower()
            if not normalized:
                continue
            if re.search(rf"\b{re.escape(normalized)}\b", text):
                return True
        return False

    def flow_shock_reasons(
        self,
        *,
        max_volume_change_1h_pct: float,
        max_abs_one_hour_price_change: float,
        min_liquidity: float,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.liquidity is not None and self.liquidity < min_liquidity:
            reasons.append("thin_liquidity")
        if (
            self.volume_change_1h_pct is not None
            and abs(self.volume_change_1h_pct) >= max_volume_change_1h_pct
        ):
            reasons.append("flow_spike")
        if (
            self.one_hour_price_change is not None
            and abs(self.one_hour_price_change) >= max_abs_one_hour_price_change
        ):
            reasons.append("price_shock")
        return tuple(reasons)

    def flow_stability_score(
        self,
        *,
        max_volume_change_1h_pct: float,
        max_abs_one_hour_price_change: float,
        min_liquidity: float,
    ) -> float:
        score = 1.0
        if self.liquidity is not None:
            if self.liquidity >= min_liquidity * 10:
                score += 0.2
            elif self.liquidity < min_liquidity:
                score -= 0.45

        if self.volume_change_1h_pct is not None:
            magnitude = abs(self.volume_change_1h_pct)
            if magnitude <= 5:
                score += 0.35
            elif magnitude <= max_volume_change_1h_pct:
                score += 0.1
            else:
                score -= 0.5

        if self.one_hour_price_change is not None:
            magnitude = abs(self.one_hour_price_change)
            if magnitude <= max_abs_one_hour_price_change / 3:
                score += 0.25
            elif magnitude <= max_abs_one_hour_price_change:
                score += 0.05
            else:
                score -= 0.6

        return round(min(max(score, 0.0), 1.5), 4)

    @classmethod
    def from_gamma_market(cls, payload: dict[str, object]) -> "Market":
        prices = _parse_sequence(payload.get("outcomePrices"), name="outcomePrices")
        token_ids = _parse_sequence(payload.get("clobTokenIds"), name="clobTokenIds")
        primary_event = _derive_primary_event(payload)

        if len(prices) != 2:
            raise ValueError("outcomePrices must contain exactly two entries")
        if len(token_ids) != 2:
            raise ValueError("clobTokenIds must contain exactly two entries")

        raw_end_date = payload.get("endDate") or payload.get("endDateIso")
        if not isinstance(raw_end_date, str):
            raise ValueError("Market payload is missing endDate/endDateIso")

        return cls(
            market_id=_as_text(payload.get("id"), "id"),
            condition_id=_as_text(payload.get("conditionId"), "conditionId"),
            question=_as_text(payload.get("question"), "question"),
            slug=_optional_text(payload.get("slug")),
            category=_derive_category(payload),
            event_id=primary_event["id"],
            event_slug=primary_event["slug"],
            event_title=primary_event["title"],
            end_date=_parse_datetime(raw_end_date),
            yes_token_id=token_ids[0],
            no_token_id=token_ids[1],
            yes_price=float(prices[0]),
            no_price=float(prices[1]),
            volume=_derive_volume(payload),
            liquidity=_optional_float(payload.get("liquidity")),
            volume_24hr=_optional_float(payload.get("volume24hr")),
            volume_change_1h_pct=_optional_float(payload.get("volume_change_1h_pct")),
            one_hour_price_change=_optional_float(payload.get("oneHourPriceChange")),
            active=bool(payload.get("active", False)),
            closed=bool(payload.get("closed", False)),
            archived=bool(payload.get("archived", False)),
        )


def _parse_sequence(value: object, *, name: str) -> list[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} must be valid JSON when provided as a string") from exc
    else:
        parsed = value

    if not isinstance(parsed, list):
        raise ValueError(f"{name} must be a list")

    return [_as_text(item, name) for item in parsed]


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require_aware_datetime(parsed, "end_date")
    return parsed


def _as_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Expected optional text field to be a string")
    text = value.strip()
    return text or None


def _as_float(value: object, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return _as_float(value, "value")


def _derive_volume(payload: dict[str, object]) -> float:
    for value in (
        payload.get("volumeNum"),
        payload.get("volume"),
    ):
        parsed = _optional_float(value)
        if parsed is not None:
            return parsed
    return 0.0


def _derive_category(payload: dict[str, object]) -> str:
    for value in (
        payload.get("category"),
        _nested_get(payload, "event", "category"),
        _nested_get(payload, "series", "category"),
        _nested_get(payload, "events", 0, "category"),
        _nested_get(payload, "events", 0, "subcategory"),
        _nested_get(payload, "tags", 0, "name"),
        _nested_get(payload, "tags", 0, "label"),
        _nested_get(payload, "tags", 0, "slug"),
    ):
        text = _optional_text(value)
        if text is not None:
            return text.lower()
    return _infer_category(payload)


def _derive_primary_event(payload: dict[str, object]) -> dict[str, str | None]:
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        return {"id": None, "slug": None, "title": None}
    event = events[0]
    if not isinstance(event, dict):
        return {"id": None, "slug": None, "title": None}
    event_id = event.get("id")
    return {
        "id": str(event_id).strip() if event_id is not None else None,
        "slug": _optional_text(event.get("slug")),
        "title": _optional_text(event.get("title")),
    }


def _infer_category(payload: dict[str, object]) -> str:
    allowed_categories = {"sports", "crypto", "politics", "unknown"}
    if payload.get("sportsMarketType") is not None:
        return "sports"

    corpus = _build_category_corpus(payload)
    text_blob = corpus["text"]
    domains = corpus["domains"]

    if _matches_any_pattern(text_blob, _UNKNOWN_BLOCKERS):
        return "unknown"

    scores = {
        "sports": 0,
        "crypto": 0,
        "politics": 0,
    }

    _apply_domain_scores(scores, domains)
    _apply_phrase_scores(scores, text_blob)
    _apply_token_scores(scores, text_blob)

    best_category = max(scores, key=scores.get)
    best_score = scores[best_category]
    sorted_scores = sorted(scores.values(), reverse=True)
    runner_up = sorted_scores[1] if len(sorted_scores) > 1 else 0

    if best_score < 3 or (best_score - runner_up) < 2:
        return "unknown"

    return best_category if best_category in allowed_categories else "unknown"


def _build_category_corpus(payload: dict[str, object]) -> dict[str, object]:
    event_texts: list[str] = []
    event_domains: set[str] = set()

    events = payload.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            event_texts.extend(
                text
                for text in (
                    _optional_text(event.get("title")),
                    _optional_text(event.get("slug")),
                    _optional_text(event.get("ticker")),
                    _optional_text(event.get("description")),
                    _optional_text(event.get("resolutionSource")),
                    _optional_text(_nested_get(event, "eventMetadata", "context_description")),
                )
                if text is not None
            )
            event_domains.update(_extract_domains(_optional_text(event.get("resolutionSource")) or ""))

    fields = [
        _optional_text(payload.get("question")),
        _optional_text(payload.get("slug")),
        _optional_text(payload.get("groupItemTitle")),
        _optional_text(payload.get("description")),
        _optional_text(payload.get("resolutionSource")),
        *event_texts,
    ]
    text_blob = " ".join(text for text in fields if text is not None).lower()
    domains = _extract_domains(text_blob)
    domains.update(event_domains)
    return {"text": text_blob, "domains": domains}


def _apply_domain_scores(scores: dict[str, int], domains: set[str]) -> None:
    for category, domain_set in _CATEGORY_DOMAINS.items():
        if domains.intersection(domain_set):
            scores[category] += 5


def _apply_phrase_scores(scores: dict[str, int], text: str) -> None:
    for category, patterns in _CATEGORY_PHRASES.items():
        for pattern in patterns:
            if re.search(pattern, text):
                scores[category] += 4


def _apply_token_scores(scores: dict[str, int], text: str) -> None:
    for category, tokens in _CATEGORY_TOKENS.items():
        matches = {
            token
            for token in tokens
            if re.search(rf"\b{re.escape(token)}\b", text)
        }
        scores[category] += len(matches) * 2


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _matches_any_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _extract_domains(text: str) -> set[str]:
    matches = re.findall(r"https?://[^\s)]+", text)
    domains: set[str] = set()
    for match in matches:
        parsed = urlparse(match)
        domain = parsed.netloc.lower().replace("www.", "")
        if domain:
            domains.add(domain)
    return domains


_UNKNOWN_BLOCKERS = (
    r"\b(highest temperature|temperature range|weather|wunderground|degrees celsius|degrees fahrenheit|°c|°f|rainfall|hurricane|forecast)\b",
    r"\b(netflix|album|movie|box office|billboard|grammys|oscars|spotify|trailer 3|season 2|top global)\b",
    r"\b(close above \$|close below \$|stock|shares|market cap|nasdaq|s&p 500|dow jones)\b",
)

_DETERMINISTIC_PATTERNS = (
    r"\bexact score\b",
    r"\bspread:\b",
    r"\bmoneyline\b",
    r"\bgame [0-9]+ winner\b",
    r"\bmap [0-9]+ winner\b",
    r"\bmatch winner\b",
    r"\bwinner\b",
    r"\bup or down\b",
    r"\b(bitcoin|btc)\b.*\b(up|down|above|below)\b",
    r"\breach \$\d",
    r"\bdip to \$\d",
    r"\babove \$\d",
    r"\bbelow \$\d",
    r"\bplayoffs\b",
    r"\bdivision\b",
    r"\bregular season\b",
)

_QUALITATIVE_RISK_PATTERNS = (
    ("social", r"\b(tweet|tweets|tweeted|twitter|x\.com|instagram|tiktok|podcast|interview|statement|announce|announces|announced)\b"),
    ("narrative", r"\b(fired|resigned|quit|arrested|indicted|sued|lawsuit|divorce|breakup|marry|married|dating|hospitalized|dies|death)\b"),
    ("opinion", r"\b(say|says|said|claims|claimed|believes|thinks|thought)\b"),
)

_CATEGORY_DOMAINS = {
    "sports": {
        "nba.com",
        "nhl.com",
        "mlb.com",
        "nfl.com",
        "pgatour.com",
        "masters.com",
        "atptour.com",
        "wtatennis.com",
        "ufc.com",
        "fifa.com",
    },
    "crypto": {
        "coinmarketcap.com",
        "coingecko.com",
        "etherscan.io",
        "mempool.space",
        "defillama.com",
        "binance.com",
    },
    "politics": {
        "elections.ap.org",
        "fec.gov",
        "realclearpolling.com",
        "decisiondeskhq.com",
        "fivethirtyeight.com",
    },
}

_CATEGORY_PHRASES = {
    "sports": (
        r"\bmasters tournament\b",
        r"\bnba playoffs\b",
        r"\bnhl regular season\b",
        r"\blead the nba in\b",
        r"\bregular season games\b",
        r"\bwin the [a-z ]+ division\b",
        r"\bspread:\b",
        r"\bmoneyline\b",
        r"\bbo[135]\b",
        r"\bvct\b",
    ),
    "crypto": (
        r"\bbitcoin reach\b",
        r"\bethereum reach\b",
        r"\bprice of (bitcoin|btc|ethereum|eth|xrp|solana|sol)\b",
        r"\b(bitcoin|btc|ethereum|eth|xrp|solana|sol) (up|down)\b",
        r"\bup or down\b.*\b(bitcoin|btc|ethereum|eth|xrp|solana|sol)\b",
    ),
    "politics": (
        r"\bout as president\b",
        r"\bpresidential election\b",
        r"\bcontrol of the senate\b",
        r"\bcontrol of the house\b",
        r"\bprime minister\b",
        r"\bmayoral election\b",
        r"\bgubernatorial election\b",
    ),
}

_CATEGORY_TOKENS = {
    "sports": (
        "nba",
        "nhl",
        "nfl",
        "mlb",
        "ufc",
        "ncaa",
        "pgatour",
        "masters",
        "playoffs",
        "division",
        "esports",
        "valorant",
        "rebounds",
        "steals",
        "touchdowns",
    ),
    "crypto": (
        "bitcoin",
        "btc",
        "ethereum",
        "eth",
        "solana",
        "sol",
        "xrp",
        "dogecoin",
        "doge",
        "litecoin",
        "ltc",
        "cardano",
        "ada",
        "bnb",
        "avax",
    ),
    "politics": (
        "election",
        "senate",
        "congress",
        "governor",
        "mayor",
        "parliament",
        "referendum",
        "president",
        "presidential",
        "trump",
        "biden",
        "vote",
        "poll",
    ),
}


def _nested_get(value: object, *path: object) -> object | None:
    current = value
    for item in path:
        if isinstance(item, int):
            if not isinstance(current, list) or item >= len(current):
                return None
            current = current[item]
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(item)
    return current


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")


def _require_aware_datetime(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware")
