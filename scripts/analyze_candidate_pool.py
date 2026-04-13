from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.ai_scorer import AIScorer
from api.gamma import GammaClient
from bot.candidate_pool import (
    apply_candidate_caps,
    build_candidate_clusters,
    build_template_clusters,
    distinct_ranked_candidates,
)
from bot.config import load_environment, load_runtime_config
from bot.catalyst import load_active_catalyst_snapshot
from bot.spot import load_active_spot_snapshot
from bot.ranker import EdgeRanker
from bot.scanner import MarketScanner
from bot.supervisor import evaluate_candidates
from bot.tracker import TradeTracker


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze the current candidate pool, including distinct opportunity count and concentration."
    )
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--clusters", type=int, default=10)
    parser.add_argument(
        "--strategy-section",
        default="research_strategy",
        help="Which strategy profile to analyze. Defaults to research_strategy.",
    )
    return parser.parse_args()


async def main_async(top: int, clusters: int, strategy_section: str) -> dict[str, object]:
    env = load_environment()
    runtime = load_runtime_config(strategy_section=strategy_section)
    tracker = TradeTracker(env.database_url)
    tracker.initialize()
    spot_snapshot = await load_active_spot_snapshot(env=env, runtime=runtime, tracker=tracker)
    catalyst_snapshot = await load_active_catalyst_snapshot(env=env, runtime=runtime, tracker=tracker)
    async with GammaClient() as gamma:
        scanner = MarketScanner(gamma, runtime.strategy)
        markets = await gamma.fetch_all_open_markets()

    decisions = scanner.diagnose_markets(
        markets,
        spot_snapshot=spot_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )
    qualifying = [decision.market for decision in decisions if decision.qualifies]
    ranker = EdgeRanker(runtime.strategy)
    breakdowns = [
        ranker.score_breakdown(
            market,
            spot_snapshot=spot_snapshot,
            catalyst_snapshot=catalyst_snapshot,
        )
        for market in qualifying
    ]
    ranked = ranker.rank_markets(
        qualifying,
        spot_snapshot=spot_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )
    distinct_ranked = distinct_ranked_candidates(ranked)
    scored = evaluate_candidates(
        ranked,
        ai_scorer=AIScorer(runtime.ai_scoring),
        min_score=runtime.strategy.min_score,
    )
    capped_candidates = apply_candidate_caps(
        scored,
        max_per_event=runtime.strategy.max_per_event_candidates,
        max_per_template=runtime.strategy.max_per_template_candidates,
        exact_score_max_per_event=runtime.strategy.exact_score_max_per_event,
    )
    candidate_clusters = build_candidate_clusters(ranked)

    return {
        "strategy_section": strategy_section,
        "scanner_candidate_count": len(qualifying),
        "ranked_candidate_count": len(ranked),
        "distinct_ranked_candidate_count": len(distinct_ranked),
        "capped_candidate_count": len(capped_candidates),
        "raw_score_distribution": _raw_score_distribution(breakdowns),
        "threshold_pass_counts": _threshold_pass_counts(breakdowns),
        "average_score_components": _average_score_components(breakdowns),
        "dominant_drags_for_failed_markets": _dominant_drags(
            breakdowns,
            min_score=runtime.strategy.min_score,
        ),
        "category_breakdown": Counter(item.ranked_market.market.category for item in capped_candidates).most_common(),
        "score_band_breakdown": _score_bands(capped_candidates),
        "largest_clusters": [item.as_dict() for item in candidate_clusters[: max(1, clusters)]],
        "largest_template_clusters": [item.as_dict() for item in build_template_clusters(ranked)[: max(1, clusters)]],
        "top_distinct_candidates": [_scored_candidate_to_dict(item) for item in capped_candidates[: max(1, top)]],
    }


def _score_bands(ranked) -> dict[str, int]:
    bands = {
        "7.0-7.99": 0,
        "8.0-8.99": 0,
        "9.0-10.0": 0,
    }
    for item in ranked:
        score = getattr(item, "final_score", None)
        if score is None:
            score = item.score
        if score < 8.0:
            bands["7.0-7.99"] += 1
        elif score < 9.0:
            bands["8.0-8.99"] += 1
        else:
            bands["9.0-10.0"] += 1
    return bands


def _raw_score_distribution(breakdowns) -> dict[str, int]:
    bands = {
        "<5.0": 0,
        "5.0-5.99": 0,
        "6.0-6.99": 0,
        "7.0-7.49": 0,
        "7.5-7.99": 0,
        "8.0+": 0,
    }
    for item in breakdowns:
        score = item.final_score
        if score < 5.0:
            bands["<5.0"] += 1
        elif score < 6.0:
            bands["5.0-5.99"] += 1
        elif score < 7.0:
            bands["6.0-6.99"] += 1
        elif score < 7.5:
            bands["7.0-7.49"] += 1
        elif score < 8.0:
            bands["7.5-7.99"] += 1
        else:
            bands["8.0+"] += 1
    return bands


def _threshold_pass_counts(breakdowns) -> dict[str, int]:
    return {
        ">=7.0": sum(1 for item in breakdowns if item.final_score >= 7.0),
        ">=7.25": sum(1 for item in breakdowns if item.final_score >= 7.25),
        ">=7.5": sum(1 for item in breakdowns if item.final_score >= 7.5),
    }


def _average_score_components(breakdowns) -> dict[str, float]:
    if not breakdowns:
        return {
            "price_score": 0.0,
            "time_score": 0.0,
            "volume_score": 0.0,
            "spread_score": 0.0,
            "category_multiplier": 0.0,
            "base_score": 0.0,
            "final_score": 0.0,
        }
    count = len(breakdowns)
    return {
        "price_score": round(sum(item.price_score for item in breakdowns) / count, 4),
        "time_score": round(sum(item.time_score for item in breakdowns) / count, 4),
        "volume_score": round(sum(item.volume_score for item in breakdowns) / count, 4),
        "spread_score": round(sum(item.spread_score for item in breakdowns) / count, 4),
        "category_multiplier": round(sum(item.category_multiplier for item in breakdowns) / count, 4),
        "base_score": round(sum(item.base_score for item in breakdowns) / count, 4),
        "final_score": round(sum(item.final_score for item in breakdowns) / count, 4),
    }


def _dominant_drags(breakdowns, *, min_score: float) -> dict[str, int]:
    failed = [item for item in breakdowns if item.final_score < min_score]
    counts = Counter(item.dominant_drag for item in failed)
    return dict(counts.most_common())


def _ranked_market_to_dict(item) -> dict[str, object]:
    market = item.market
    return {
        "market_id": market.market_id,
        "event_id": market.event_id,
        "event_slug": market.event_slug,
        "event_title": market.event_title,
        "slug": market.slug,
        "question": market.question,
        "category": market.category,
        "score": item.score,
        "selected_side": item.selected_side.value,
        "selected_price": item.selected_price,
        "hours_to_close": item.hours_to_close,
        "volume": market.volume,
    }


def _scored_candidate_to_dict(item) -> dict[str, object]:
    payload = _ranked_market_to_dict(item.ranked_market)
    payload["final_score"] = item.final_score
    return payload


def main() -> None:
    args = parse_args()
    payload = asyncio.run(main_async(args.top, args.clusters, args.strategy_section))
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
