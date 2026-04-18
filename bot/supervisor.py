from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from api.ai_scorer import AIScoreResult, AIScorer
from api.clob import build_clob_client
from api.gamma import GammaClient
from api.spot import SpotSnapshot
from bot.autoscale import AutoScaleEngine
from bot.candidate_pool import (
    apply_candidate_caps,
    build_candidate_clusters,
    build_template_clusters,
    candidate_cluster_key,
    template_cluster_key,
)
from bot.config import EnvironmentConfig, RuntimeConfig, resolve_execution_style
from bot.catalyst import load_active_catalyst_snapshot
from bot.exit_manager import try_auto_exit_position
from bot.executor import LiveOrderPreview, OrderExecutor
from bot.order_monitor import monitor_order_status
from bot.process_lock import acquire_database_lock
from bot.reconciler import reconcile_manual_exits, reconcile_open_orders, reconcile_open_positions
from bot.spot import load_active_spot_snapshot, load_active_spot_snapshots, resolve_spot_snapshot_for_market
from bot.ranker import EdgeRanker, RankedMarket
from bot.risk import KillSignal, RiskManager
from bot.runtime_state import send_optional_alert, sync_live_state
from bot.scanner import MarketScanner, ScanDecision
from bot.tracker import TradeTracker


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    ranked_market: RankedMarket
    final_score: float
    ai_result: AIScoreResult | None

    def as_dict(self) -> dict[str, object]:
        payload = {
            "market_id": self.ranked_market.market.market_id,
            "slug": self.ranked_market.market.slug,
            "question": self.ranked_market.market.question,
            "rules_score": self.ranked_market.score,
            "final_score": self.final_score,
            "selected_side": self.ranked_market.selected_side.value,
            "selected_price": self.ranked_market.selected_price,
            "hours_to_close": self.ranked_market.hours_to_close,
            "volume": self.ranked_market.market.volume,
            "category": self.ranked_market.market.category,
        }
        if self.ai_result is not None:
            payload["ai"] = {
                "ai_score": self.ai_result.ai_score,
                "blended_score": self.ai_result.blended_score,
                "source": self.ai_result.source,
                "rationale": self.ai_result.rationale,
            }
        return payload


@dataclass(frozen=True, slots=True)
class PreparedCycle:
    state_id: int
    bankroll: float
    phase: int
    budget_cap: float
    selected_budget: float
    kill_signal: KillSignal | None
    skip_reason: str | None
    reconciled_positions: tuple[dict[str, object], ...]
    top_candidates: tuple[ScoredCandidate, ...]
    near_miss_candidates: tuple[dict[str, object], ...]
    preview: LiveOrderPreview | None
    raw_candidate_count: int = 0
    distinct_candidate_count: int = 0
    capped_candidate_count: int = 0
    candidate_clusters: tuple[dict[str, object], ...] = ()
    template_clusters: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "state_id": self.state_id,
            "bankroll": self.bankroll,
            "phase": self.phase,
            "budget_cap": self.budget_cap,
            "selected_budget": self.selected_budget,
            "reconciled_positions": list(self.reconciled_positions),
            "top_candidates": [item.as_dict() for item in self.top_candidates],
            "near_miss_candidates": list(self.near_miss_candidates),
            "preview": None if self.preview is None else self.preview.as_dict(),
            "raw_candidate_count": self.raw_candidate_count,
            "distinct_candidate_count": self.distinct_candidate_count,
            "capped_candidate_count": self.capped_candidate_count,
            "candidate_clusters": list(self.candidate_clusters),
            "template_clusters": list(self.template_clusters),
        }
        if self.kill_signal is not None:
            payload["kill_signal"] = {
                "level": self.kill_signal.level,
                "reason": self.kill_signal.reason,
                "pause_minutes": self.kill_signal.pause_minutes,
                "manual_resume_required": self.kill_signal.manual_resume_required,
            }
        if self.skip_reason is not None:
            payload["skip_reason"] = self.skip_reason
        return payload


@dataclass(frozen=True, slots=True)
class ExecutedCycle:
    prepared: PreparedCycle
    submit_response: dict[str, object] | None
    cancel_response: dict[str, object] | None
    final_order_status: dict[str, object] | None
    tracked_trade_id: int | None
    position_id: int | None
    trade_outcome: str | None
    state_id: int | None
    exit_result: dict[str, object] | None = None
    execution_status: str | None = None
    timing: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        payload = {"prepared": self.prepared.as_dict()}
        if self.submit_response is not None:
            payload["submit_response"] = self.submit_response
        if self.cancel_response is not None:
            payload["cancel_response"] = self.cancel_response
        if self.final_order_status is not None:
            payload["final_order_status"] = self.final_order_status
        if self.tracked_trade_id is not None:
            payload["tracked_trade_id"] = self.tracked_trade_id
        if self.position_id is not None:
            payload["position_id"] = self.position_id
        if self.trade_outcome is not None:
            payload["trade_outcome"] = self.trade_outcome
        if self.state_id is not None:
            payload["post_trade_state_id"] = self.state_id
        if self.exit_result is not None:
            payload["exit_result"] = self.exit_result
        if self.execution_status is not None:
            payload["execution_status"] = self.execution_status
        if self.timing is not None:
            payload["timing"] = self.timing
        return payload


@dataclass
class TradeQuotaState:
    target_trades: int
    executed_trades: int = 0

    @property
    def reached(self) -> bool:
        return self.executed_trades >= self.target_trades


def determine_live_budget_ceiling(
    *,
    bankroll: float,
    runtime: RuntimeConfig,
    requested_budget: float | None,
) -> float:
    autoscale = AutoScaleEngine()
    autoscale_cap = autoscale.get_max_position_size(bankroll)
    max_position_usd = _profile_or_global(
        runtime.strategy.max_position_usd,
        runtime.sizing.max_position_usd,
    )
    max_position_pct = _profile_or_global(
        runtime.strategy.max_position_pct,
        runtime.sizing.max_position_pct,
    )
    bankroll_floor_for_live = _profile_or_global(
        runtime.strategy.bankroll_floor_for_live,
        runtime.sizing.bankroll_floor_for_live,
    )
    hard_cap = min(
        max_position_usd,
        bankroll * max_position_pct,
        autoscale_cap,
    )
    if bankroll < bankroll_floor_for_live:
        return 0.0
    if requested_budget is not None:
        hard_cap = min(hard_cap, requested_budget)
    if hard_cap <= 0:
        return 0.0
    return round(hard_cap, 6)


def determine_live_budget(
    *,
    bankroll: float,
    runtime: RuntimeConfig,
    requested_budget: float | None,
) -> float:
    budget_ceiling = determine_live_budget_ceiling(
        bankroll=bankroll,
        runtime=runtime,
        requested_budget=requested_budget,
    )
    if budget_ceiling <= 0:
        return 0.0
    risk_per_trade_pct = _profile_or_global(
        runtime.strategy.risk_per_trade_pct,
        runtime.sizing.risk_per_trade_pct,
    )
    kelly_target = bankroll * risk_per_trade_pct * runtime.sizing.fractional_kelly
    if requested_budget is not None:
        kelly_target = min(kelly_target, requested_budget)
    return round(min(kelly_target, budget_ceiling), 6)


def _profile_or_global(profile_value: float | None, global_value: float) -> float:
    return global_value if profile_value is None else profile_value


def _spot_latency_summary(spot_snapshot: SpotSnapshot | dict[str, SpotSnapshot] | None) -> float | None:
    if spot_snapshot is None:
        return None
    if isinstance(spot_snapshot, dict):
        latencies = [snapshot.fetch_latency_seconds for snapshot in spot_snapshot.values() if snapshot.fetch_latency_seconds is not None]
        if not latencies:
            return None
        return round(max(latencies), 6)
    return spot_snapshot.fetch_latency_seconds


def has_open_position_capacity(*, tracker: TradeTracker, runtime: RuntimeConfig) -> bool:
    return tracker.open_position_count() < runtime.execution.max_open_positions


def evaluate_candidates(
    ranked: list[RankedMarket],
    *,
    ai_scorer: AIScorer,
    min_score: float,
) -> list[ScoredCandidate]:
    candidates: list[ScoredCandidate] = []
    for item in ranked:
        ai_result = ai_scorer.score_market(item.market, rules_score=item.score)
        final_score = ai_result.blended_score if ai_result is not None else item.score
        if final_score < min_score:
            continue
        candidates.append(
            ScoredCandidate(
                ranked_market=item,
                final_score=round(final_score, 2),
                ai_result=ai_result,
            )
        )
    return sorted(
        candidates,
        key=lambda item: (item.final_score, item.ranked_market.score),
        reverse=True,
    )


def distinct_scored_candidates(
    candidates: list[ScoredCandidate],
    *,
    max_per_cluster: int = 1,
) -> list[ScoredCandidate]:
    if max_per_cluster <= 0:
        raise ValueError("max_per_cluster must be positive")
    selected: list[ScoredCandidate] = []
    counts: dict[str, int] = {}
    for item in candidates:
        key = candidate_cluster_key(item.ranked_market)
        seen = counts.get(key, 0)
        if seen >= max_per_cluster:
            continue
        counts[key] = seen + 1
        selected.append(item)
    return selected


async def prepare_supervised_cycle(
    *,
    env: EnvironmentConfig,
    runtime: RuntimeConfig,
    tracker: TradeTracker,
    spot_snapshot: SpotSnapshot | dict[str, SpotSnapshot] | None,
    catalyst_snapshot,
    requested_budget: float | None,
    limit_price: float | None,
    top: int,
    near_misses: int,
):
    client = build_clob_client(env, include_api_creds=True)
    async with GammaClient() as gamma:
        reconciled = await reconcile_open_positions(tracker, gamma)
        manual_exits = await reconcile_manual_exits(
            tracker,
            gamma,
            clob_client=client,
            wallet_address=env.poly_wallet_address,
        )
        reconciled.extend(manual_exits)
        if reconciled:
            send_optional_alert(
                env,
                "\n".join(
                    [
                        "Supervised runner reconciled positions:",
                        *[
                            f"{item.market_id} {item.outcome.value} pnl=${item.pnl:.2f}"
                            for item in reconciled
                        ],
                    ]
                ),
                level="INFO",
            )
        state_sync = sync_live_state(
            tracker=tracker,
            runtime=runtime,
            env=env,
            clob_client=client,
        )
        scanner = MarketScanner(gamma, runtime.strategy)
        markets = await scanner.load_markets()
    risk_manager = RiskManager()
    kill_signal = risk_manager.check_all_kills(state_sync.state)
    if kill_signal is not None:
        pause_until = (
            None
            if kill_signal.pause_minutes < 0
            else datetime.now(UTC) + timedelta(minutes=kill_signal.pause_minutes)
        )
        paused_state = tracker.build_state_snapshot(
            bankroll=state_sync.bankroll,
            phase=state_sync.state.phase,
            strategy_min_price=runtime.strategy.min_price,
            strategy_min_score=runtime.strategy.min_score,
            strategy_name=runtime.strategy.name,
            is_paused=True,
            pause_level=kill_signal.level,
            pause_reason=kill_signal.reason,
            pause_until=pause_until,
        )
        paused_state_id = tracker.record_state(paused_state)
        send_optional_alert(
            env,
            f"Supervised runner paused by {kill_signal.level}: {kill_signal.reason}",
            level="KILL",
        )
        return PreparedCycle(
            state_id=paused_state_id,
            bankroll=state_sync.bankroll,
            phase=paused_state.phase,
            budget_cap=0.0,
            selected_budget=0.0,
            kill_signal=kill_signal,
            skip_reason=kill_signal.reason,
            reconciled_positions=tuple(_reconcile_result_to_dict(item) for item in reconciled),
            top_candidates=(),
            near_miss_candidates=(),
            preview=None,
        )

    decisions = scanner.diagnose_markets(
        markets,
        spot_snapshot=spot_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )
    qualifying = [decision.market for decision in decisions if decision.qualifies]
    ranker = EdgeRanker(runtime.strategy)
    ranked = ranker.rank_markets(
        qualifying,
        spot_snapshot=spot_snapshot,
        catalyst_snapshot=catalyst_snapshot,
    )
    ai_scorer = AIScorer(runtime.ai_scoring, api_key=env.anthropic_api_key)
    candidates = evaluate_candidates(
        ranked,
        ai_scorer=ai_scorer,
        min_score=runtime.strategy.min_score,
    )
    distinct_candidates = distinct_scored_candidates(candidates)
    capped_candidates = apply_candidate_caps(
        candidates,
        max_per_event=runtime.strategy.max_per_event_candidates,
        max_per_template=runtime.strategy.max_per_template_candidates,
        exact_score_max_per_event=runtime.strategy.exact_score_max_per_event,
    )
    candidate_clusters = tuple(
        cluster.as_dict() for cluster in build_candidate_clusters([item.ranked_market for item in candidates])[:10]
    )
    template_clusters = tuple(
        cluster.as_dict() for cluster in build_template_clusters([item.ranked_market for item in candidates])[:10]
    )
    tracker.record_candidate_snapshots(
        [
            _candidate_snapshot_payload(
                item,
                timestamp=datetime.now(UTC),
                selected=index == 0,
            )
            for index, item in enumerate(capped_candidates)
        ]
    )

    budget_ceiling = determine_live_budget_ceiling(
        bankroll=state_sync.bankroll,
        runtime=runtime,
        requested_budget=requested_budget,
    )
    budget = determine_live_budget(
        bankroll=state_sync.bankroll,
        runtime=runtime,
        requested_budget=requested_budget,
    )
    budget_cap = budget_ceiling

    if not has_open_position_capacity(tracker=tracker, runtime=runtime):
        return PreparedCycle(
            state_id=state_sync.state_id,
            bankroll=state_sync.bankroll,
            phase=state_sync.state.phase,
            budget_cap=budget_cap,
            selected_budget=0.0,
            kill_signal=None,
            skip_reason=(
                f"Open position cap reached ({runtime.execution.max_open_positions}); "
                "no new trade will be opened this cycle."
            ),
            reconciled_positions=tuple(_reconcile_result_to_dict(item) for item in reconciled),
            top_candidates=(),
            near_miss_candidates=(),
            preview=None,
            raw_candidate_count=0,
            distinct_candidate_count=0,
            capped_candidate_count=0,
            candidate_clusters=(),
            template_clusters=(),
        )

    if budget <= 0:
        return PreparedCycle(
            state_id=state_sync.state_id,
            bankroll=state_sync.bankroll,
            phase=state_sync.state.phase,
            budget_cap=budget_cap,
            selected_budget=0.0,
            kill_signal=None,
            skip_reason="Current bankroll phase does not permit supervised live positions yet.",
            reconciled_positions=tuple(_reconcile_result_to_dict(item) for item in reconciled),
            top_candidates=tuple(capped_candidates[: max(1, top)]),
            near_miss_candidates=tuple(
                _near_miss_candidates(
                    decisions,
                    ranker,
                    top=max(1, near_misses),
                    spot_snapshot=spot_snapshot,
                    catalyst_snapshot=catalyst_snapshot,
                )
            ),
            preview=None,
            raw_candidate_count=len(candidates),
            distinct_candidate_count=len(distinct_candidates),
            capped_candidate_count=len(capped_candidates),
            candidate_clusters=candidate_clusters,
            template_clusters=template_clusters,
        )

    if not capped_candidates:
        return PreparedCycle(
            state_id=state_sync.state_id,
            bankroll=state_sync.bankroll,
            phase=state_sync.state.phase,
            budget_cap=budget_cap,
            selected_budget=budget,
            kill_signal=None,
            skip_reason="No currently qualifying ranked markets were found.",
            reconciled_positions=tuple(_reconcile_result_to_dict(item) for item in reconciled),
            top_candidates=(),
            near_miss_candidates=tuple(
                _near_miss_candidates(
                    decisions,
                    ranker,
                    top=max(1, near_misses),
                    spot_snapshot=spot_snapshot,
                    catalyst_snapshot=catalyst_snapshot,
                )
            ),
            preview=None,
            raw_candidate_count=len(candidates),
            distinct_candidate_count=len(distinct_candidates),
            capped_candidate_count=0,
            candidate_clusters=candidate_clusters,
            template_clusters=template_clusters,
        )

    executor = OrderExecutor(client, execution_style=resolve_execution_style(runtime))
    try:
        preview = executor.preview_limit_buy(
            capped_candidates[0].ranked_market.market,
            side=capped_candidates[0].ranked_market.selected_side,
            budget_usdc=budget,
            limit_price=limit_price,
            use_minimum_budget=runtime.execution.dynamic_min_position,
        )
    except ValueError as exc:
        return PreparedCycle(
            state_id=state_sync.state_id,
            bankroll=state_sync.bankroll,
            phase=state_sync.state.phase,
            budget_cap=budget_cap,
            selected_budget=budget,
            kill_signal=None,
            skip_reason=str(exc),
            reconciled_positions=tuple(_reconcile_result_to_dict(item) for item in reconciled),
            top_candidates=tuple(capped_candidates[: max(1, top)]),
            near_miss_candidates=tuple(
                _near_miss_candidates(
                    decisions,
                    ranker,
                    top=max(1, near_misses),
                    spot_snapshot=spot_snapshot,
                    catalyst_snapshot=catalyst_snapshot,
                )
            ),
            preview=None,
            raw_candidate_count=len(candidates),
            distinct_candidate_count=len(distinct_candidates),
            capped_candidate_count=len(capped_candidates),
            candidate_clusters=candidate_clusters,
            template_clusters=template_clusters,
        )

    minimum_budget = preview.minimum_budget_usdc
    if minimum_budget is None:
        minimum_budget = round(preview.limit_price * runtime.sizing.min_valid_order_shares, 6)
    if minimum_budget > budget_cap + 1e-9:
        return PreparedCycle(
            state_id=state_sync.state_id,
            bankroll=state_sync.bankroll,
            phase=state_sync.state.phase,
            budget_cap=budget_cap,
            selected_budget=budget,
            kill_signal=None,
            skip_reason=(
                f"Exchange minimum ${minimum_budget:.2f} exceeds risk ceiling ${budget_cap:.2f}."
            ),
            reconciled_positions=tuple(_reconcile_result_to_dict(item) for item in reconciled),
            top_candidates=tuple(capped_candidates[: max(1, top)]),
            near_miss_candidates=tuple(
                _near_miss_candidates(
                    decisions,
                    ranker,
                    top=max(1, near_misses),
                    spot_snapshot=spot_snapshot,
                    catalyst_snapshot=catalyst_snapshot,
                )
            ),
            preview=preview,
            raw_candidate_count=len(candidates),
            distinct_candidate_count=len(distinct_candidates),
            capped_candidate_count=len(capped_candidates),
            candidate_clusters=candidate_clusters,
            template_clusters=template_clusters,
        )
    if budget < minimum_budget:
        budget = minimum_budget

    return PreparedCycle(
        state_id=state_sync.state_id,
        bankroll=state_sync.bankroll,
        phase=state_sync.state.phase,
        budget_cap=budget_cap,
        selected_budget=budget,
        kill_signal=None,
        skip_reason=None,
        reconciled_positions=tuple(_reconcile_result_to_dict(item) for item in reconciled),
        top_candidates=tuple(capped_candidates[: max(1, top)]),
        near_miss_candidates=tuple(
            _near_miss_candidates(
                decisions,
                ranker,
                top=max(1, near_misses),
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
            )
        ),
        preview=preview,
        raw_candidate_count=len(candidates),
        distinct_candidate_count=len(distinct_candidates),
        capped_candidate_count=len(capped_candidates),
        candidate_clusters=candidate_clusters,
        template_clusters=template_clusters,
    )


async def _reconcile_open_orders_before_cycle(
    *,
    env: EnvironmentConfig,
    tracker: TradeTracker,
):
    client = build_clob_client(env, include_api_creds=True)
    async with GammaClient() as gamma:
        return await reconcile_open_orders(tracker, gamma, client)


async def _load_cycle_spot_snapshot(
    *,
    env: EnvironmentConfig,
    runtime: RuntimeConfig,
    tracker: TradeTracker,
):
    if runtime.strategy.spot_symbols:
        return await load_active_spot_snapshots(env=env, runtime=runtime, tracker=tracker)
    return await load_active_spot_snapshot(env=env, runtime=runtime, tracker=tracker)


async def _load_cycle_catalyst_snapshot(
    *,
    env: EnvironmentConfig,
    runtime: RuntimeConfig,
    tracker: TradeTracker,
):
    return await load_active_catalyst_snapshot(env=env, runtime=runtime, tracker=tracker)


def execute_prepared_cycle(
    *,
    prepared: PreparedCycle,
    env: EnvironmentConfig,
    runtime: RuntimeConfig,
    tracker: TradeTracker,
    spot_snapshot: SpotSnapshot | dict[str, SpotSnapshot] | None,
    catalyst_snapshot,
    monitor_seconds: int,
    poll_interval: float,
    cancel_if_open: bool,
) -> ExecutedCycle:
    if prepared.preview is None or prepared.skip_reason is not None:
        return ExecutedCycle(
            prepared=prepared,
            submit_response=None,
            cancel_response=None,
            final_order_status=None,
            tracked_trade_id=None,
            position_id=None,
            trade_outcome=None,
            state_id=None,
            exit_result=None,
            execution_status=None,
            timing=None,
        )

    client = build_clob_client(env, include_api_creds=True)
    executor = OrderExecutor(client, execution_style=resolve_execution_style(runtime))
    selected = prepared.top_candidates[0]

    submitted_at = datetime.now(UTC)
    submit_response = executor.place_limit_buy(prepared.preview)
    signal_to_submit_seconds = None
    spot_fetch_latency_seconds = None
    if spot_snapshot is not None:
        selected_spot_snapshot = resolve_spot_snapshot_for_market(
            selected.ranked_market.market,
            spot_snapshot,
            available_symbols=runtime.strategy.spot_symbols,
        )
        if selected_spot_snapshot is None and isinstance(spot_snapshot, dict) and spot_snapshot:
            selected_spot_snapshot = next(iter(spot_snapshot.values()))
        if selected_spot_snapshot is not None:
            signal_to_submit_seconds = round((submitted_at - selected_spot_snapshot.observed_at).total_seconds(), 6)
            spot_fetch_latency_seconds = selected_spot_snapshot.fetch_latency_seconds
    order_id = executor.extract_order_id(submit_response)
    if not order_id:
        raise ValueError("Could not extract order id from submit response")
    tracker.record_order(
        order_id=order_id,
        market_id=selected.ranked_market.market.market_id,
        strategy_name=runtime.strategy.name,
        status="OPEN",
        requested_size=prepared.preview.budget_usdc,
        limit_price=prepared.preview.limit_price,
        filled_size=0.0,
        last_seen_status="OPEN",
        last_seen_at=submitted_at,
        exchange_payload={
            "submit_response": submit_response,
            "preview": prepared.preview.as_dict(),
            "selected_candidate": selected.as_dict(),
        },
    )
    send_optional_alert(
        env,
        (
            f"Supervised order submitted for {prepared.preview.question}\n"
            f"Order ID: {order_id}\n"
            f"Side: {prepared.preview.outcome_side}\n"
            f"Budget: ${prepared.preview.budget_usdc:.2f}\n"
            f"Final score: {selected.final_score:.2f}"
            + (
                ""
                if signal_to_submit_seconds is None
                else f"\nSignal-to-submit: {signal_to_submit_seconds:.3f}s"
            )
        ),
        level="INFO",
    )

    final_status = monitor_order_status(
        env=env,
        executor=executor,
        order_id=order_id,
        market_id=selected.ranked_market.market.condition_id,
        timeout_seconds=monitor_seconds,
        poll_interval=poll_interval,
        heartbeat_seconds=runtime.execution.websocket_heartbeat_seconds,
    )
    cancel_response = None
    if not final_status.is_terminal and cancel_if_open:
        cancel_response = executor.cancel_order(order_id)
        final_status = executor.get_order_status(order_id)
    final_status = executor.reconcile_fill_status(
        prepared.preview,
        final_status,
        submitted_at=submitted_at,
    )

    if final_status.has_fill:
        tracker.update_order_fill(
            order_id=order_id,
            market_id=selected.ranked_market.market.market_id,
            strategy_name=runtime.strategy.name,
            requested_size=prepared.preview.budget_usdc,
            limit_price=prepared.preview.limit_price,
            filled_size=final_status.matched_size,
            last_seen_status=final_status.status,
            last_seen_at=final_status.created_at or submitted_at,
            exchange_payload={
                "final_status": {
                    "order_id": final_status.order_id,
                    "status": final_status.status,
                    "matched_size": final_status.matched_size,
                    "original_size": final_status.original_size,
                    "price": final_status.price,
                }
            },
        )
    elif final_status.is_terminal:
        tracker.close_order(
            order_id=order_id,
            strategy_name=runtime.strategy.name,
            status=final_status.status,
            last_seen_status=final_status.status,
            last_seen_at=final_status.created_at or submitted_at,
            filled_size=final_status.matched_size,
            exchange_payload={
                "final_status": {
                    "order_id": final_status.order_id,
                    "status": final_status.status,
                    "matched_size": final_status.matched_size,
                    "original_size": final_status.original_size,
                    "price": final_status.price,
                }
            },
        )
    else:
        tracker.record_order(
            order_id=order_id,
            market_id=selected.ranked_market.market.market_id,
            strategy_name=runtime.strategy.name,
            status=final_status.status,
            requested_size=prepared.preview.budget_usdc,
            limit_price=prepared.preview.limit_price,
            filled_size=final_status.matched_size,
            last_seen_status=final_status.status,
            last_seen_at=final_status.created_at or submitted_at,
            exchange_payload={
                "final_status": {
                    "order_id": final_status.order_id,
                    "status": final_status.status,
                    "matched_size": final_status.matched_size,
                    "original_size": final_status.original_size,
                    "price": final_status.price,
                }
            },
        )

    if final_status.has_fill:
        trade = executor.build_trade_record(
            selected.ranked_market.market,
            prepared.preview,
            final_status,
            strategy_name=runtime.strategy.name,
        )
        trade_id = tracker.record_trade(
            trade,
            notes=json.dumps(
                {
                    "selected_candidate": selected.as_dict(),
                    "preview": prepared.preview.as_dict(),
                    "final_status": {
                        "order_id": final_status.order_id,
                        "status": final_status.status,
                        "matched_size": final_status.matched_size,
                        "original_size": final_status.original_size,
                        "price": final_status.price,
                    },
                }
            ),
        )
        position_id = tracker.register_open_position_from_trade(trade)
        trade_outcome = trade.outcome.value
    else:
        trade = None
        trade_id = None
        position_id = None
        trade_outcome = None

    state_sync = sync_live_state(
        tracker=tracker,
        runtime=runtime,
        env=env,
        clob_client=client,
    )
    if final_status.has_fill:
        alert_level = "WIN"
        alert_message = (
            f"Supervised order finished for {prepared.preview.question}\n"
            f"Order ID: {final_status.order_id}\n"
            f"Status: {final_status.status}\n"
            f"Matched size: {final_status.matched_size:.2f}\n"
            f"Tracked trade id: {trade_id}"
        )
    elif final_status.status in {"LIVE", "LIVE_RESTING"}:
        alert_level = "INFO"
        alert_message = (
            f"Supervised order still resting for {prepared.preview.question}\n"
            f"Order ID: {final_status.order_id}\n"
            f"Status: {final_status.status}\n"
            f"Matched size: {final_status.matched_size:.2f}"
        )
    else:
        alert_level = "WARN"
        alert_message = (
            f"Supervised order finished for {prepared.preview.question}\n"
            f"Order ID: {final_status.order_id}\n"
            f"Status: {final_status.status}\n"
            f"Matched size: {final_status.matched_size:.2f}\n"
            f"Tracked trade id: {trade_id}"
        )
    send_optional_alert(env, alert_message, level=alert_level)

    return ExecutedCycle(
        prepared=prepared,
        submit_response=submit_response,
        cancel_response=cancel_response,
        final_order_status={
            "order_id": final_status.order_id,
            "status": final_status.status,
            "matched_size": final_status.matched_size,
            "original_size": final_status.original_size,
            "price": final_status.price,
        },
        tracked_trade_id=trade_id,
        position_id=position_id,
        trade_outcome=trade_outcome,
        state_id=state_sync.state_id,
        exit_result=None,
        execution_status="FILLED"
        if final_status.has_fill
        else ("RESTING" if final_status.status in {"LIVE", "LIVE_RESTING"} else final_status.status),
        timing={
            "spot_fetch_latency_seconds": spot_fetch_latency_seconds,
            "catalyst_fetch_latency_seconds": catalyst_snapshot.fetch_latency_seconds if catalyst_snapshot is not None else None,
            "signal_to_submit_seconds": signal_to_submit_seconds,
            "submitted_at": submitted_at.astimezone(UTC).isoformat(),
        },
    )


def run_supervised_loop(
    *,
    env: EnvironmentConfig,
    runtime: RuntimeConfig,
    cycles: int,
    requested_budget: float | None,
    limit_price: float | None,
    top: int,
    near_misses: int,
    submit: bool,
    sleep_seconds: float,
    monitor_seconds: int,
    poll_interval: float,
    cancel_if_open: bool,
    quota_state: TradeQuotaState | None = None,
) -> list[ExecutedCycle]:
    if cycles <= 0:
        raise ValueError("cycles must be positive")
    tracker = TradeTracker(env.database_url)
    tracker.initialize()
    if quota_state is None and runtime.strategy.trade_quota_enabled:
        quota_state = TradeQuotaState(target_trades=runtime.strategy.trade_quota_target_trades or 0)
    results: list[ExecutedCycle] = []
    for index in range(cycles):
        results.append(
            run_supervised_cycle(
                env=env,
                runtime=runtime,
                tracker=tracker,
                requested_budget=requested_budget,
                limit_price=limit_price,
                top=top,
                near_misses=near_misses,
                submit=submit,
                monitor_seconds=monitor_seconds,
                poll_interval=poll_interval,
                cancel_if_open=cancel_if_open,
                quota_state=quota_state,
            )
        )
        if index < cycles - 1:
            time.sleep(max(0.0, sleep_seconds))
    return results


def run_supervised_cycle(
    *,
    env: EnvironmentConfig,
    runtime: RuntimeConfig,
    tracker: TradeTracker,
    requested_budget: float | None,
    limit_price: float | None,
    top: int,
    near_misses: int,
    submit: bool,
    monitor_seconds: int,
    poll_interval: float,
    cancel_if_open: bool,
    quota_state: TradeQuotaState | None = None,
) -> ExecutedCycle:
    with acquire_database_lock(env.database_url):
        control = tracker.resolve_control_state(runtime.strategy.name)
        latest_state = tracker.get_latest_state(strategy_name=runtime.strategy.name) or tracker.get_latest_state()
        if control.should_stop():
            pause_reason = "Stopped via Telegram command"
            paused_state = tracker.build_state_snapshot(
                bankroll=0.0 if latest_state is None else latest_state.bankroll,
                phase=0 if latest_state is None else latest_state.phase,
                strategy_min_price=runtime.strategy.min_price,
                strategy_min_score=runtime.strategy.min_score,
                strategy_name=runtime.strategy.name,
                open_orders=tracker.open_order_count(strategy_name=runtime.strategy.name),
                open_positions=tracker.open_position_count(strategy_name=runtime.strategy.name),
                is_paused=True,
                pause_level="USER",
                pause_reason=pause_reason,
                pause_until=None,
            )
            paused_state_id = tracker.record_state(paused_state)
            send_optional_alert(
                env,
                f"Supervised runner stopped by Telegram: {runtime.strategy.name}",
                level="INFO",
            )
            prepared = PreparedCycle(
                state_id=paused_state_id,
                bankroll=paused_state.bankroll,
                phase=paused_state.phase,
                budget_cap=0.0,
                selected_budget=0.0,
                kill_signal=None,
                skip_reason=pause_reason,
                reconciled_positions=(),
                top_candidates=(),
                near_miss_candidates=(),
                preview=None,
            )
            return ExecutedCycle(
                prepared=prepared,
                submit_response=None,
                cancel_response=None,
                final_order_status=None,
                tracked_trade_id=None,
                position_id=None,
                trade_outcome=None,
                state_id=paused_state_id,
                exit_result=None,
                execution_status="STOPPED",
                timing={
                    "spot_fetch_latency_seconds": None,
                    "catalyst_fetch_latency_seconds": None,
                    "signal_to_submit_seconds": None,
                },
            )
        if control.should_pause() and control.run_once_pending <= 0:
            pause_reason = "Paused via Telegram command"
            paused_state = tracker.build_state_snapshot(
                bankroll=0.0 if latest_state is None else latest_state.bankroll,
                phase=0 if latest_state is None else latest_state.phase,
                strategy_min_price=runtime.strategy.min_price,
                strategy_min_score=runtime.strategy.min_score,
                strategy_name=runtime.strategy.name,
                open_orders=tracker.open_order_count(strategy_name=runtime.strategy.name),
                open_positions=tracker.open_position_count(strategy_name=runtime.strategy.name),
                is_paused=True,
                pause_level="USER",
                pause_reason=pause_reason,
                pause_until=None,
            )
            paused_state_id = tracker.record_state(paused_state)
            prepared = PreparedCycle(
                state_id=paused_state_id,
                bankroll=paused_state.bankroll,
                phase=paused_state.phase,
                budget_cap=0.0,
                selected_budget=0.0,
                kill_signal=None,
                skip_reason=pause_reason,
                reconciled_positions=(),
                top_candidates=(),
                near_miss_candidates=(),
                preview=None,
            )
            return ExecutedCycle(
                prepared=prepared,
                submit_response=None,
                cancel_response=None,
                final_order_status=None,
                tracked_trade_id=None,
                position_id=None,
                trade_outcome=None,
                state_id=paused_state_id,
                exit_result=None,
                execution_status="PAUSED",
                timing={
                    "spot_fetch_latency_seconds": None,
                    "catalyst_fetch_latency_seconds": None,
                    "signal_to_submit_seconds": None,
                },
            )
        if control.run_once_pending > 0:
            tracker.consume_run_once(runtime.strategy.name)
        asyncio.run(
            _reconcile_open_orders_before_cycle(
                env=env,
                tracker=tracker,
            )
        )
        spot_snapshot = asyncio.run(
            _load_cycle_spot_snapshot(
                env=env,
                runtime=runtime,
                tracker=tracker,
            )
        )
        catalyst_snapshot = asyncio.run(
            _load_cycle_catalyst_snapshot(
                env=env,
                runtime=runtime,
                tracker=tracker,
            )
        )
        if submit:
            exit_result = try_auto_exit_position(
                env=env,
                runtime=runtime,
                tracker=tracker,
                monitor_seconds=monitor_seconds,
                poll_interval=poll_interval,
                cancel_if_open=cancel_if_open,
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
            )
            if exit_result is not None:
                latest_state = tracker.get_latest_state()
                prepared = PreparedCycle(
                    state_id=exit_result.state_id or 0,
                    bankroll=0.0 if latest_state is None else latest_state.bankroll,
                    phase=0 if latest_state is None else latest_state.phase,
                    budget_cap=0.0,
                    selected_budget=0.0,
                    kill_signal=None,
                    skip_reason="Auto-exit executed before new entries were evaluated.",
                    reconciled_positions=(),
                    top_candidates=(),
                    near_miss_candidates=(),
                    preview=None,
                )
                return ExecutedCycle(
                    prepared=prepared,
                    submit_response=None,
                    cancel_response=None,
                    final_order_status=None,
                    tracked_trade_id=None,
                    position_id=exit_result.position_id,
                    trade_outcome="WIN" if (exit_result.pnl or 0.0) > 0 else "LOSS" if exit_result.pnl is not None else None,
                    state_id=exit_result.state_id,
                    exit_result=exit_result.as_dict(),
                    execution_status="EXIT",
                    timing={
                        "spot_fetch_latency_seconds": _spot_latency_summary(spot_snapshot),
                        "catalyst_fetch_latency_seconds": catalyst_snapshot.fetch_latency_seconds if catalyst_snapshot is not None else None,
                        "signal_to_submit_seconds": None,
                    },
                )
        if quota_state is not None and quota_state.reached:
            open_orders = tracker.open_order_count(strategy_name=runtime.strategy.name)
            open_positions = tracker.open_position_count(strategy_name=runtime.strategy.name)
            latest_state_for_quota = tracker.get_latest_state(strategy_name=runtime.strategy.name) or tracker.get_latest_state()
            quota_reason = (
                f"Trade quota reached ({quota_state.executed_trades}/{quota_state.target_trades}); "
                "draining open positions before shutdown."
            )
            draining_state = tracker.build_state_snapshot(
                bankroll=0.0 if latest_state_for_quota is None else latest_state_for_quota.bankroll,
                phase=0 if latest_state_for_quota is None else latest_state_for_quota.phase,
                strategy_min_price=runtime.strategy.min_price,
                strategy_min_score=runtime.strategy.min_score,
                strategy_name=runtime.strategy.name,
                open_orders=open_orders,
                open_positions=open_positions,
                is_paused=True,
                pause_level="SYSTEM",
                pause_reason=quota_reason,
                pause_until=None,
            )
            draining_state_id = tracker.record_state(draining_state)
            if open_orders == 0 and open_positions == 0:
                tracker.upsert_control_state(
                    profile_name=runtime.strategy.name,
                    desired_state="STOPPED",
                    run_once_pending=0,
                    notes="Trade quota reached and profile drained",
                )
                prepared = PreparedCycle(
                    state_id=draining_state_id,
                    bankroll=draining_state.bankroll,
                    phase=draining_state.phase,
                    budget_cap=0.0,
                    selected_budget=0.0,
                    kill_signal=None,
                    skip_reason=quota_reason,
                    reconciled_positions=(),
                    top_candidates=(),
                    near_miss_candidates=(),
                    preview=None,
                )
                return ExecutedCycle(
                    prepared=prepared,
                    submit_response=None,
                    cancel_response=None,
                    final_order_status=None,
                    tracked_trade_id=None,
                    position_id=None,
                    trade_outcome=None,
                    state_id=draining_state_id,
                    exit_result=None,
                    execution_status="STOPPED",
                    timing={
                        "spot_fetch_latency_seconds": _spot_latency_summary(spot_snapshot),
                        "catalyst_fetch_latency_seconds": catalyst_snapshot.fetch_latency_seconds if catalyst_snapshot is not None else None,
                        "signal_to_submit_seconds": None,
                    },
                )
            prepared = PreparedCycle(
                state_id=draining_state_id,
                bankroll=draining_state.bankroll,
                phase=draining_state.phase,
                budget_cap=0.0,
                selected_budget=0.0,
                kill_signal=None,
                skip_reason=quota_reason,
                reconciled_positions=(),
                top_candidates=(),
                near_miss_candidates=(),
                preview=None,
            )
            return ExecutedCycle(
                prepared=prepared,
                submit_response=None,
                cancel_response=None,
                final_order_status=None,
                tracked_trade_id=None,
                position_id=None,
                trade_outcome=None,
                state_id=draining_state_id,
                exit_result=None,
                execution_status="QUOTA_HOLD",
                timing={
                    "spot_fetch_latency_seconds": _spot_latency_summary(spot_snapshot),
                    "catalyst_fetch_latency_seconds": catalyst_snapshot.fetch_latency_seconds if catalyst_snapshot is not None else None,
                    "signal_to_submit_seconds": None,
                },
            )

        prepared = asyncio.run(
            prepare_supervised_cycle(
                env=env,
                runtime=runtime,
                tracker=tracker,
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
                requested_budget=requested_budget,
                limit_price=limit_price,
                top=top,
                near_misses=near_misses,
            )
        )
        if submit:
            result = execute_prepared_cycle(
                prepared=prepared,
                env=env,
                runtime=runtime,
                tracker=tracker,
                spot_snapshot=spot_snapshot,
                catalyst_snapshot=catalyst_snapshot,
                monitor_seconds=monitor_seconds,
                poll_interval=poll_interval,
                cancel_if_open=cancel_if_open,
            )
            if quota_state is not None and result.tracked_trade_id is not None:
                quota_state.executed_trades += 1
            return result
        return ExecutedCycle(
            prepared=prepared,
            submit_response=None,
            cancel_response=None,
            final_order_status=None,
            tracked_trade_id=None,
            position_id=None,
            trade_outcome=None,
            state_id=None,
            exit_result=None,
            execution_status=None,
            timing={
                "spot_fetch_latency_seconds": _spot_latency_summary(spot_snapshot),
                "catalyst_fetch_latency_seconds": catalyst_snapshot.fetch_latency_seconds if catalyst_snapshot is not None else None,
                "signal_to_submit_seconds": None,
            },
        )


def _near_miss_candidates(
    decisions: list[ScanDecision],
    ranker: EdgeRanker,
    *,
    top: int,
    spot_snapshot=None,
    catalyst_snapshot=None,
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for decision in decisions:
        if decision.qualifies:
            continue
        score = ranker.score_market(
            decision.market,
            spot_snapshot=spot_snapshot,
            catalyst_snapshot=catalyst_snapshot,
        )
        candidates.append(
            {
                "market_id": decision.market.market_id,
                "slug": decision.market.slug,
                "question": decision.market.question,
                "score_if_scored": score,
                "selected_side": decision.market.near_certain_side.value,
                "selected_price": decision.market.near_certain_price,
                "hours_to_close": round(decision.hours_to_close, 4),
                "volume": decision.market.volume,
                "category": decision.market.category,
                "failed_rules": list(decision.reasons),
            }
        )
    candidates.sort(key=lambda item: (len(item["failed_rules"]), -item["score_if_scored"]))
    return candidates[:top]


def _candidate_snapshot_payload(
    candidate: ScoredCandidate,
    *,
    timestamp: datetime,
    selected: bool,
) -> dict[str, object]:
    market = candidate.ranked_market.market
    return {
        "timestamp": timestamp.astimezone(UTC).isoformat(),
        "market_id": market.market_id,
        "event_id": market.event_id,
        "event_slug": market.event_slug,
        "event_title": market.event_title,
        "cluster_key": candidate_cluster_key(candidate.ranked_market),
        "template_key": template_cluster_key(candidate.ranked_market),
        "market_question": market.question,
        "category": market.category,
        "score": candidate.ranked_market.score,
        "final_score": candidate.final_score,
        "selected_side": candidate.ranked_market.selected_side.value,
        "selected_price": candidate.ranked_market.selected_price,
        "hours_to_close": candidate.ranked_market.hours_to_close,
        "volume": market.volume,
        "selected": int(selected),
        "notes": json.dumps(candidate.as_dict()),
    }


def _reconcile_result_to_dict(item) -> dict[str, object]:
    return {
        "position_id": item.position_id,
        "order_id": item.order_id,
        "market_id": item.market_id,
        "resolution_price": item.resolution_price,
        "pnl": item.pnl,
        "outcome": item.outcome.value,
    }
