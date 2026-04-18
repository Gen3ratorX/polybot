from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from api.catalyst import CatalystSnapshot
from api.spot import SpotSnapshot
from bot.config import load_runtime_config
from bot.exit_manager import ExitResult, find_exit_candidate, try_auto_exit_position
from bot.executor import LiveOrderStatus
from bot.tracker import TradeTracker
from models import OutcomeSide, Trade, TradeOutcome


def test_try_auto_exit_position_closes_full_winning_position(tmp_path, monkeypatch) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'exit.db'}")
    tracker.initialize()
    trade = Trade(
        timestamp=datetime(2026, 4, 7, 16, 0, tzinfo=UTC),
        market_id="m1",
        market_question="Question",
        category="sports",
        side=OutcomeSide.NO,
        entry_price=0.96,
        position_size=4.992,
        order_id="entry-oid",
        fill_price=0.96,
        fill_time=datetime(2026, 4, 7, 16, 0, 5, tzinfo=UTC),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(trade)
    tracker.register_open_position_from_trade(trade)

    runtime = load_runtime_config("config.yaml")
    env = SimpleNamespace(database_url=f"sqlite:///{tmp_path / 'exit.db'}")

    market = SimpleNamespace(
        market_id="m1",
        condition_id="cond-m1",
        question="Question",
        yes_price=0.0005,
        no_price=0.9995,
        yes_token_id="yes-token",
        no_token_id="no-token",
        token_id_for=lambda side: "no-token" if side is OutcomeSide.NO else "yes-token",
    )
    async def fake_find_exit_candidate(tracker, runtime, spot_snapshot=None):
        return tracker.list_open_positions()[0], market

    monkeypatch.setattr("bot.exit_manager.find_exit_candidate", fake_find_exit_candidate)

    class FakeClient:
        pass

    class FakeExecutor:
        def __init__(self, client, **kwargs):
            self.client = client
            self.kwargs = kwargs

        def preview_limit_sell(self, market, *, side, shares, limit_price=None):
            return SimpleNamespace(
                market_id=market.market_id,
                question=market.question,
                outcome_side=side.value,
                token_id="no-token",
                shares=shares,
                limit_price=0.999,
                midpoint=0.9995,
                best_bid=0.999,
                best_ask=1.0,
                tick_size=0.001,
                estimated_proceeds_usdc=round(shares * 0.999, 6),
            )

        def place_limit_sell(self, preview):
            return {"orderID": "exit-oid", "status": "live"}

        def extract_order_id(self, payload):
            return payload["orderID"]

        def cancel_order(self, order_id):
            return {"canceled": [order_id]}

        def get_order_status(self, order_id):
            return LiveOrderStatus(
                order_id=order_id,
                status="MATCHED",
                created_at=datetime(2026, 4, 7, 16, 10, tzinfo=UTC),
                market_condition_id="cond-m1",
                token_id="no-token",
                side="SELL",
                price=0.999,
                original_size=5.2,
                matched_size=5.2,
            )

        def reconcile_fill_status(self, preview, order_status, *, submitted_at, side="SELL"):
            return order_status

    monkeypatch.setattr("bot.exit_manager.build_clob_client", lambda env, include_api_creds=True: FakeClient())
    monkeypatch.setattr("bot.exit_manager.OrderExecutor", FakeExecutor)
    monkeypatch.setattr("bot.exit_manager.monitor_order_status", lambda **kwargs: kwargs["executor"].get_order_status(kwargs["order_id"]))
    monkeypatch.setattr("bot.exit_manager.send_optional_alert", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "bot.exit_manager.sync_live_state",
        lambda **kwargs: SimpleNamespace(state_id=7, bankroll=6.0, state=SimpleNamespace(open_positions=0)),
    )

    result = try_auto_exit_position(
        env=env,  # type: ignore[arg-type]
        runtime=runtime,
        tracker=tracker,
        monitor_seconds=20,
        poll_interval=1.0,
        cancel_if_open=False,
    )

    assert isinstance(result, ExitResult)
    assert result is not None
    assert result.fully_closed is True
    assert result.pnl == round((5.2 * 0.999) - 4.992, 6)
    assert tracker.open_position_count() == 0
    updated_trade = tracker.list_recent_trades(limit=1)[0]
    assert updated_trade.outcome is TradeOutcome.WIN


def test_try_auto_exit_position_reduces_partial_exit_and_persists_ledger(tmp_path, monkeypatch) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'exit-partial.db'}")
    tracker.initialize()
    trade = Trade(
        timestamp=datetime(2026, 4, 7, 16, 0, tzinfo=UTC),
        market_id="m1",
        market_question="Question",
        category="sports",
        side=OutcomeSide.NO,
        entry_price=0.96,
        position_size=11.52,
        order_id="entry-oid",
        fill_price=0.96,
        fill_time=datetime(2026, 4, 7, 16, 0, 5, tzinfo=UTC),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(trade)
    tracker.register_open_position_from_trade(trade)

    runtime = load_runtime_config("config.yaml")
    env = SimpleNamespace(database_url=f"sqlite:///{tmp_path / 'exit-partial.db'}")

    market = SimpleNamespace(
        market_id="m1",
        condition_id="cond-m1",
        question="Question",
        yes_price=0.0005,
        no_price=0.9995,
        yes_token_id="yes-token",
        no_token_id="no-token",
        token_id_for=lambda side: "no-token" if side is OutcomeSide.NO else "yes-token",
    )

    async def fake_find_exit_candidate(tracker, runtime, spot_snapshot=None, catalyst_snapshot=None):
        return tracker.list_open_positions()[0], market

    monkeypatch.setattr("bot.exit_manager.find_exit_candidate", fake_find_exit_candidate)

    class FakeClient:
        pass

    class FakeExecutor:
        def __init__(self, client, **kwargs):
            self.client = client
            self.kwargs = kwargs
            self.preview_calls: list[float] = []
            self._order_ids = iter(["exit-1", "exit-2"])
            self._statuses = {
                "exit-1": LiveOrderStatus(
                    order_id="exit-1",
                    status="MATCHED",
                    created_at=datetime(2026, 4, 7, 16, 10, tzinfo=UTC),
                    market_condition_id="cond-m1",
                    token_id="no-token",
                    side="SELL",
                    price=0.999,
                    original_size=12.0,
                    matched_size=6.0,
                ),
                "exit-2": LiveOrderStatus(
                    order_id="exit-2",
                    status="MATCHED",
                    created_at=datetime(2026, 4, 7, 16, 12, tzinfo=UTC),
                    market_condition_id="cond-m1",
                    token_id="no-token",
                    side="SELL",
                    price=0.999,
                    original_size=6.0,
                    matched_size=6.0,
                ),
            }

        def preview_limit_sell(self, market, *, side, shares, limit_price=None):
            self.preview_calls.append(shares)
            return SimpleNamespace(
                market_id=market.market_id,
                question=market.question,
                outcome_side=side.value,
                token_id="no-token",
                shares=shares,
                limit_price=0.999,
                midpoint=0.9995,
                best_bid=0.999,
                best_ask=1.0,
                tick_size=0.001,
                estimated_proceeds_usdc=round(shares * 0.999, 6),
            )

        def place_limit_sell(self, preview):
            return {"orderID": next(self._order_ids), "status": "live"}

        def extract_order_id(self, payload):
            return payload["orderID"]

        def cancel_order(self, order_id):
            return {"canceled": [order_id]}

        def get_order_status(self, order_id):
            return self._statuses[order_id]

        def reconcile_fill_status(self, preview, order_status, *, submitted_at, side="SELL"):
            return order_status

        def get_order_book_snapshot(self, token_id):
            return SimpleNamespace(token_id=token_id, midpoint=0.9995, best_bid=0.999, best_ask=1.0, tick_size=0.001, min_order_size=5.0)

    fake_executor = FakeExecutor(FakeClient())
    monkeypatch.setattr("bot.exit_manager.build_clob_client", lambda env, include_api_creds=True: FakeClient())
    monkeypatch.setattr("bot.exit_manager.OrderExecutor", lambda client, **kwargs: fake_executor)
    monkeypatch.setattr("bot.exit_manager.monitor_order_status", lambda **kwargs: kwargs["executor"].get_order_status(kwargs["order_id"]))
    monkeypatch.setattr("bot.exit_manager.send_optional_alert", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "bot.exit_manager.sync_live_state",
        lambda **kwargs: SimpleNamespace(state_id=7, bankroll=6.0, state=SimpleNamespace(open_positions=0)),
    )

    result = try_auto_exit_position(
        env=env,  # type: ignore[arg-type]
        runtime=runtime,
        tracker=tracker,
        monitor_seconds=20,
        poll_interval=1.0,
        cancel_if_open=False,
    )

    assert isinstance(result, ExitResult)
    assert result is not None
    assert result.fully_closed is True
    assert fake_executor.preview_calls == [12.0, 6.0]
    assert tracker.open_position_count() == 0

    first_order = tracker.get_order_by_order_id("exit-1")
    second_order = tracker.get_order_by_order_id("exit-2")
    assert first_order is not None
    assert second_order is not None
    assert first_order["status"] == "PARTIALLY_FILLED"
    assert first_order["filled_size"] == 6.0
    assert first_order["exchange_payload"]["exit_reason"] == "take_profit"
    assert second_order["status"] == "FILLED"
    assert second_order["filled_size"] == 6.0

    from bot.reconciler import reconcile_open_orders

    class FakeGammaClient:
        async def fetch_market(self, market_id: str):
            return market

    class FakeClobClient:
        funder = "0xabc"

        def __init__(self) -> None:
            self._order_payloads = {
                "exit-1": {
                    "id": "exit-1",
                    "status": "CANCELED",
                    "createdAtMs": int(datetime(2026, 4, 7, 16, 10, tzinfo=UTC).timestamp() * 1000),
                    "assetId": "no-token",
                    "side": "SELL",
                    "price": "0.999",
                    "originalSize": "12.0",
                    "sizeMatched": "6.0",
                    "market_id": "m1",
                }
            }
            self._trades_by_token = {
                "no-token": [
                    {
                        "trader_side": "MAKER",
                        "match_time": str(int(datetime(2026, 4, 7, 16, 10, tzinfo=UTC).timestamp())),
                        "maker_orders": [
                            {
                                "maker_address": "0xabc",
                                "asset_id": "no-token",
                                "side": "SELL",
                                "matched_amount": "6.0",
                                "price": "0.999",
                            }
                        ],
                    }
                ]
            }

        def get_order(self, order_id: str) -> dict[str, object]:
            return self._order_payloads[order_id]

        def get_trades(self, params=None):
            token_id = getattr(params, "asset_id", None)
            if token_id is None:
                trades: list[dict[str, object]] = []
                for payloads in self._trades_by_token.values():
                    trades.extend(payloads)
                return trades
            return list(self._trades_by_token.get(str(token_id), []))

    import asyncio

    results = asyncio.run(reconcile_open_orders(tracker, FakeGammaClient(), FakeClobClient()))
    assert len(results) == 1
    assert results[0].previous_status == "PARTIALLY_FILLED"
    assert results[0].current_status == "PARTIALLY_FILLED"
    assert tracker.get_order_by_order_id("exit-1")["status"] == "PARTIALLY_FILLED"


def test_find_exit_candidate_triggers_thesis_break_stop_loss(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'exit-stop-loss.db'}")
    tracker.initialize()
    trade = Trade(
        timestamp=datetime(2026, 4, 7, 13, 0, tzinfo=UTC),
        market_id="m1",
        market_question="Question",
        category="sports",
        side=OutcomeSide.YES,
        entry_price=0.92,
        position_size=4.6,
        order_id="entry-oid",
        fill_price=0.92,
        fill_time=datetime(2026, 4, 7, 13, 0, 5, tzinfo=UTC),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(trade)
    tracker.register_open_position_from_trade(trade)

    runtime = load_runtime_config("config.yaml")
    market = SimpleNamespace(
        market_id="m1",
        condition_id="cond-m1",
        question="Question",
        yes_price=0.84,
        no_price=0.16,
        yes_token_id="yes-token",
        no_token_id="no-token",
        token_id_for=lambda side: "yes-token" if side is OutcomeSide.YES else "no-token",
    )

    class FakeGammaClient:
        async def fetch_market(self, market_id: str):
            return market

    async def run_check():
        return await find_exit_candidate(tracker, runtime)

    # patch the module-level GammaClient context used by find_exit_candidate
    from bot import exit_manager as exit_manager_module

    class FakeGammaContext:
        async def __aenter__(self):
            return FakeGammaClient()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    original_gamma = exit_manager_module.GammaClient
    exit_manager_module.GammaClient = lambda: FakeGammaContext()
    try:
        candidate = asyncio.run(run_check())
    finally:
        exit_manager_module.GammaClient = original_gamma

    assert candidate is not None
    assert candidate[0].market_id == "m1"


def test_find_exit_candidate_triggers_btc_momentum_stop_loss(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'exit-btc-stop-loss.db'}")
    tracker.initialize()
    trade = Trade(
        timestamp=datetime(2026, 4, 7, 13, 0, tzinfo=UTC),
        market_id="btc1",
        market_question="Will Bitcoin be above 70k?",
        category="crypto",
        side=OutcomeSide.YES,
        entry_price=0.56,
        position_size=2.8,
        order_id="btc-entry-oid",
        fill_price=0.56,
        fill_time=datetime(2026, 4, 7, 13, 0, 5, tzinfo=UTC),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(trade)
    tracker.register_open_position_from_trade(trade)

    runtime = load_runtime_config("config.yaml", strategy_section="btc_up_down")
    market = SimpleNamespace(
        market_id="btc1",
        condition_id="cond-btc1",
        question="Will Bitcoin be above 70k?",
        yes_price=0.42,
        no_price=0.58,
        yes_token_id="yes-token",
        no_token_id="no-token",
        token_id_for=lambda side: "yes-token" if side is OutcomeSide.YES else "no-token",
    )

    class FakeGammaClient:
        async def fetch_market(self, market_id: str):
            return market

    async def run_check():
        return await find_exit_candidate(tracker, runtime)

    from bot import exit_manager as exit_manager_module

    class FakeGammaContext:
        async def __aenter__(self):
            return FakeGammaClient()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    original_gamma = exit_manager_module.GammaClient
    exit_manager_module.GammaClient = lambda: FakeGammaContext()
    try:
        candidate = asyncio.run(run_check())
    finally:
        exit_manager_module.GammaClient = original_gamma

    assert candidate is not None
    assert candidate[0].market_id == "btc1"


def test_find_exit_candidate_uses_spot_reversal_for_btc_stop_loss(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'exit-btc-spot.db'}")
    tracker.initialize()
    trade = Trade(
        timestamp=datetime.now(UTC) - timedelta(minutes=1),
        market_id="btc2",
        market_question="Will Bitcoin be above 70k?",
        category="crypto",
        side=OutcomeSide.YES,
        entry_price=0.56,
        position_size=2.8,
        order_id="btc-entry-oid-2",
        fill_price=0.56,
        fill_time=datetime.now(UTC) - timedelta(minutes=1, seconds=10),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(trade)
    tracker.register_open_position_from_trade(trade)

    runtime = load_runtime_config("config.yaml", strategy_section="btc_up_down")
    market = SimpleNamespace(
        market_id="btc2",
        condition_id="cond-btc2",
        question="Will Bitcoin be above 70k?",
        yes_price=0.55,
        no_price=0.45,
        yes_token_id="yes-token",
        no_token_id="no-token",
        token_id_for=lambda side: "yes-token" if side is OutcomeSide.YES else "no-token",
    )
    spot_snapshot = SpotSnapshot(
        symbol="XBTUSD",
        pair="XBTUSD",
        spot_price=70500.0,
        return_15m_pct=-0.004,
        return_1h_pct=0.012,
        fetch_latency_seconds=0.2,
        observed_at=datetime.now(UTC),
        age_seconds=20.0,
        source="kraken",
        payload=None,
    )

    class FakeGammaClient:
        async def fetch_market(self, market_id: str):
            return market

    async def run_check():
        return await find_exit_candidate(tracker, runtime, spot_snapshot=spot_snapshot)

    from bot import exit_manager as exit_manager_module

    class FakeGammaContext:
        async def __aenter__(self):
            return FakeGammaClient()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    original_gamma = exit_manager_module.GammaClient
    exit_manager_module.GammaClient = lambda: FakeGammaContext()
    try:
        candidate = asyncio.run(run_check())
    finally:
        exit_manager_module.GammaClient = original_gamma

    assert candidate is not None
    assert candidate[0].market_id == "btc2"


def test_find_exit_candidate_exits_when_btc_catalyst_is_inactive(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'exit-btc-catalyst.db'}")
    tracker.initialize()
    trade = Trade(
        timestamp=datetime.now(UTC) - timedelta(minutes=25),
        market_id="btc3",
        market_question="Will Bitcoin be above 70k?",
        category="crypto",
        side=OutcomeSide.YES,
        entry_price=0.56,
        position_size=2.8,
        order_id="btc-entry-oid-3",
        fill_price=0.56,
        fill_time=datetime.now(UTC) - timedelta(minutes=25, seconds=10),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(trade)
    tracker.register_open_position_from_trade(trade)

    runtime = load_runtime_config("config.yaml", strategy_section="btc_up_down")
    market = SimpleNamespace(
        market_id="btc3",
        condition_id="cond-btc3",
        question="Will Bitcoin be above 70k?",
        yes_price=0.55,
        no_price=0.45,
        yes_token_id="yes-token",
        no_token_id="no-token",
        token_id_for=lambda side: "yes-token" if side is OutcomeSide.YES else "no-token",
    )
    spot_snapshot = SpotSnapshot(
        symbol="XBTUSD",
        pair="XBTUSD",
        spot_price=70500.0,
        return_15m_pct=0.001,
        return_1h_pct=0.002,
        fetch_latency_seconds=0.2,
        observed_at=datetime.now(UTC),
        age_seconds=20.0,
        source="kraken",
        payload=None,
    )
    catalyst_snapshot = CatalystSnapshot(
        provider="trading_economics",
        observed_at=datetime.now(UTC),
        fetch_latency_seconds=0.1,
        events=(),
        active_events=(),
        payload={"event_count": 0, "active_event_count": 0},
    )

    class FakeGammaClient:
        async def fetch_market(self, market_id: str):
            return market

    async def run_check():
        return await find_exit_candidate(
            tracker,
            runtime,
            spot_snapshot=spot_snapshot,
            catalyst_snapshot=catalyst_snapshot,
        )

    from bot import exit_manager as exit_manager_module

    class FakeGammaContext:
        async def __aenter__(self):
            return FakeGammaClient()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    original_gamma = exit_manager_module.GammaClient
    exit_manager_module.GammaClient = lambda: FakeGammaContext()
    try:
        candidate = asyncio.run(run_check())
    finally:
        exit_manager_module.GammaClient = original_gamma

    assert candidate is not None
    assert candidate[0].market_id == "btc3"


def test_find_exit_candidate_routes_correct_spot_snapshot_for_hourly_multi_asset_profile(tmp_path) -> None:
    tracker = TradeTracker(f"sqlite:///{tmp_path / 'exit-hourly-multi.db'}")
    tracker.initialize()
    trade = Trade(
        timestamp=datetime.now(UTC) - timedelta(minutes=10),
        market_id="eth1",
        market_question="Will Ethereum be above 3500?",
        category="crypto",
        side=OutcomeSide.YES,
        entry_price=0.52,
        position_size=2.6,
        order_id="eth-entry-oid",
        fill_price=0.52,
        fill_time=datetime.now(UTC) - timedelta(minutes=10, seconds=5),
        outcome=TradeOutcome.PENDING,
        paper_trade=False,
    )
    tracker.record_trade(trade)
    tracker.register_open_position_from_trade(trade)

    runtime = load_runtime_config("config.yaml", strategy_section="hourly_momentum_multi_asset")
    market = SimpleNamespace(
        market_id="eth1",
        condition_id="cond-eth1",
        question="Will Ethereum be above 3500?",
        yes_price=0.41,
        no_price=0.59,
        yes_token_id="eth-yes",
        no_token_id="eth-no",
        strategy_text_corpus=lambda: "Will Ethereum be above 3500? ethereum eth",
        token_id_for=lambda side: "eth-yes" if side is OutcomeSide.YES else "eth-no",
    )
    spot_snapshots = {
        "xbtusd": SpotSnapshot(
            symbol="XBTUSD",
            pair="XBTUSD",
            spot_price=70500.0,
            return_15m_pct=0.0001,
            return_1h_pct=0.001,
            fetch_latency_seconds=0.1,
            observed_at=datetime.now(UTC),
            age_seconds=5.0,
            source="kraken",
            payload=None,
        ),
        "ethusd": SpotSnapshot(
            symbol="ETHUSD",
            pair="ETHUSD",
            spot_price=3600.0,
            return_15m_pct=-0.006,
            return_1h_pct=-0.012,
            fetch_latency_seconds=0.1,
            observed_at=datetime.now(UTC),
            age_seconds=5.0,
            source="kraken",
            payload=None,
        ),
        "solusd": SpotSnapshot(
            symbol="SOLUSD",
            pair="SOLUSD",
            spot_price=150.0,
            return_15m_pct=0.0002,
            return_1h_pct=0.001,
            fetch_latency_seconds=0.1,
            observed_at=datetime.now(UTC),
            age_seconds=5.0,
            source="kraken",
            payload=None,
        ),
    }

    class FakeGammaClient:
        async def fetch_market(self, market_id: str):
            return market

    async def run_check():
        return await find_exit_candidate(tracker, runtime, spot_snapshot=spot_snapshots)

    from bot import exit_manager as exit_manager_module

    class FakeGammaContext:
        async def __aenter__(self):
            return FakeGammaClient()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    original_gamma = exit_manager_module.GammaClient
    exit_manager_module.GammaClient = lambda: FakeGammaContext()
    try:
        candidate = asyncio.run(run_check())
    finally:
        exit_manager_module.GammaClient = original_gamma

    assert candidate is not None
    assert candidate[0].market_id == "eth1"
