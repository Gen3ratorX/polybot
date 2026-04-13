from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from bot.ranker import RankedMarket


@dataclass(frozen=True, slots=True)
class CandidateCluster:
    cluster_key: str
    label: str
    category: str
    size: int
    top_score: float
    market_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "cluster_key": self.cluster_key,
            "label": self.label,
            "category": self.category,
            "size": self.size,
            "top_score": self.top_score,
            "market_ids": list(self.market_ids),
        }


def distinct_ranked_candidates(
    ranked: list[RankedMarket],
    *,
    max_per_cluster: int = 1,
) -> list[RankedMarket]:
    if max_per_cluster <= 0:
        raise ValueError("max_per_cluster must be positive")
    selected: list[RankedMarket] = []
    seen: Counter[str] = Counter()
    for item in ranked:
        key = candidate_cluster_key(item)
        if seen[key] >= max_per_cluster:
            continue
        seen[key] += 1
        selected.append(item)
    return selected


def apply_candidate_caps(
    items: list[object],
    *,
    max_per_event: int,
    max_per_template: int,
    exact_score_max_per_event: int | None = None,
) -> list[object]:
    if max_per_event <= 0:
        raise ValueError("max_per_event must be positive")
    if max_per_template <= 0:
        raise ValueError("max_per_template must be positive")
    if exact_score_max_per_event is not None and exact_score_max_per_event <= 0:
        raise ValueError("exact_score_max_per_event must be positive")

    selected: list[object] = []
    event_counts: Counter[str] = Counter()
    template_counts: Counter[str] = Counter()
    for item in items:
        event_key = candidate_cluster_key(item)
        template_key = template_cluster_key(item)
        event_cap = (
            exact_score_max_per_event
            if exact_score_max_per_event is not None and template_key == "template:exact-score"
            else max_per_event
        )
        if event_counts[event_key] >= event_cap:
            continue
        if template_counts[template_key] >= max_per_template:
            continue
        event_counts[event_key] += 1
        template_counts[template_key] += 1
        selected.append(item)
    return selected


def build_candidate_clusters(ranked: list[RankedMarket]) -> list[CandidateCluster]:
    groups: dict[str, list[RankedMarket]] = defaultdict(list)
    for item in ranked:
        groups[candidate_cluster_key(item)].append(item)

    clusters: list[CandidateCluster] = []
    for key, items in groups.items():
        top_item = items[0]
        clusters.append(
            CandidateCluster(
                cluster_key=key,
                label=cluster_label(top_item),
                category=top_item.market.category,
                size=len(items),
                top_score=top_item.score,
                market_ids=tuple(item.market.market_id for item in items),
            )
        )
    clusters.sort(key=lambda item: (item.size, item.top_score), reverse=True)
    return clusters


def build_template_clusters(ranked: list[RankedMarket]) -> list[CandidateCluster]:
    groups: dict[str, list[RankedMarket]] = defaultdict(list)
    for item in ranked:
        groups[template_cluster_key(item)].append(item)

    clusters: list[CandidateCluster] = []
    for key, items in groups.items():
        top_item = items[0]
        clusters.append(
            CandidateCluster(
                cluster_key=key,
                label=template_label(top_item),
                category=top_item.market.category,
                size=len(items),
                top_score=top_item.score,
                market_ids=tuple(item.market.market_id for item in items),
            )
        )
    clusters.sort(key=lambda item: (item.size, item.top_score), reverse=True)
    return clusters


def candidate_cluster_key(item: object) -> str:
    market = _ranked_market(item).market
    if market.event_id:
        return f"event:{market.event_id}"
    if market.event_slug:
        return f"event-slug:{market.event_slug}"
    if market.slug:
        return f"slug:{_normalize_slug(market.slug)}"
    return f"question:{_normalize_question(market.question)}"


def cluster_label(item: object) -> str:
    market = _ranked_market(item).market
    return market.event_title or market.event_slug or market.slug or market.question


def template_cluster_key(item: object) -> str:
    market = _ranked_market(item).market
    text = f"{market.event_title or ''} {market.question} {market.slug or ''}".lower()
    for key, patterns in _TEMPLATE_PATTERNS:
        if any(re.search(pattern, text) for pattern in patterns):
            return f"template:{key}"
    base = market.event_title or market.question or market.slug or market.market_id
    return f"template:{_normalize_question(base)}"


def template_label(item: object) -> str:
    key = template_cluster_key(item).removeprefix("template:")
    return key.replace("-", " ")


def _ranked_market(item: object) -> RankedMarket:
    if isinstance(item, RankedMarket):
        return item
    ranked_market = getattr(item, "ranked_market", None)
    if isinstance(ranked_market, RankedMarket):
        return ranked_market
    raise TypeError(f"Unsupported candidate item type: {type(item)!r}")


def _normalize_slug(value: str) -> str:
    return re.sub(r"-\d+$", "", value.strip().lower())


def _normalize_question(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"\$[0-9,.]+", "$", text)
    text = re.sub(r"\b[0-9]+(?:\.[0-9]+)?\b", "#", text)
    text = re.sub(r"[^a-z0-9$# ]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


_TEMPLATE_PATTERNS = (
    ("exact-score", (r"\bexact score\b",)),
    ("spread-market", (r"\bspread:\b",)),
    ("price-above-threshold", (r"\bprice of\b.*\babove \$", r"\babove \$[0-9]")),
    ("price-below-threshold", (r"\bprice of\b.*\bbelow \$", r"\bbelow \$[0-9]")),
    ("reach-threshold", (r"\b(bitcoin|ethereum|xrp|solana)\b.*\breach \$",)),
    ("dip-threshold", (r"\b(bitcoin|ethereum|xrp|solana)\b.*\bdip to \$",)),
    ("match-winner", (r"\bwill .* win on [0-9-]+\b",)),
    ("division-winner", (r"\bwin the .* division\b",)),
    ("make-playoffs", (r"\bmake the .* playoffs\b",)),
    ("tournament-winner", (r"\bwin the .* tournament\b",)),
    ("season-stat-leader", (r"\blead the nba in\b", r"\bsteals during the\b",)),
    ("win-totals-over-under", (r"\bregular season games\b", r"\bover \([0-9.]+\)",)),
)
