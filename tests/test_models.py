from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from models import (
    BotState,
    Market,
    Order,
    OrderAction,
    OrderStatus,
    OutcomeSide,
    Trade,
    TradeOutcome,
)


def test_market_from_gamma_market_parses_current_fields() -> None:
    market = Market.from_gamma_market(
        {
            "id": "123",
            "conditionId": "0xcondition",
            "question": "Will team A win?",
            "slug": "team-a-win",
            "category": "sports",
            "endDate": "2026-04-06T18:00:00Z",
            "outcomePrices": '["0.91","0.09"]',
            "clobTokenIds": '["yes-token","no-token"]',
            "volume": "1234.56",
            "liquidity": "700.25",
            "volume24hr": 400,
            "oneHourPriceChange": 0.02,
            "active": True,
            "closed": False,
            "archived": False,
        }
    )

    assert market.near_certain_side is OutcomeSide.YES
    assert market.near_certain_price == 0.91
    assert market.token_id_for(OutcomeSide.NO) == "no-token"
    assert market.combined_price == 1.0


def test_market_flags_qualitative_risk_and_flow_shock() -> None:
    market = Market(
        market_id="123",
        condition_id="0xcondition",
        question="Will CEO tweet about the merger?",
        slug="ceo-tweet-merger",
        end_date=datetime.now(UTC) + timedelta(hours=2),
        yes_token_id="yes",
        no_token_id="no",
        yes_price=0.94,
        no_price=0.04,
        volume=1000,
        volume_change_1h_pct=24.0,
        one_hour_price_change=0.05,
        liquidity=1200.0,
    )

    assert market.is_deterministic_candidate() is False
    assert market.qualitative_risk_signals()
    assert market.flow_shock_reasons(
        max_volume_change_1h_pct=15.0,
        max_abs_one_hour_price_change=0.03,
        min_liquidity=2500.0,
    ) == ("thin_liquidity", "flow_spike", "price_shock")


def test_market_matches_any_keyword_in_text_corpus() -> None:
    market = Market(
        market_id="btc-1",
        condition_id="0xcondition",
        question="Will Bitcoin be above 70k?",
        slug="bitcoin-above-70k",
        end_date=datetime.now(UTC) + timedelta(hours=4),
        yes_token_id="yes",
        no_token_id="no",
        yes_price=0.55,
        no_price=0.45,
        volume=1000,
        category="crypto",
    )

    assert market.matches_any_keyword(("bitcoin", "btc")) is True
    assert market.matches_any_keyword(("ethereum", "eth")) is False


def test_market_rejects_invalid_price() -> None:
    with pytest.raises(ValueError, match="yes_price"):
        Market(
            market_id="123",
            condition_id="0xcondition",
            question="Question",
            end_date=datetime.now(UTC) + timedelta(hours=1),
            yes_token_id="yes",
            no_token_id="no",
            yes_price=1.2,
            no_price=0.1,
            volume=10,
        )


def test_trade_validates_and_settles() -> None:
    trade = Trade(
        timestamp=datetime.now(UTC),
        market_id="m1",
        market_question="Will BTC close above 100k?",
        side=OutcomeSide.YES,
        entry_price=0.9,
        position_size=9.0,
        score=8.4,
    )

    settled = trade.settle(resolution_price=1.0, execution_fee=0.1)

    assert settled.outcome is TradeOutcome.WIN
    assert settled.pnl == pytest.approx(0.9, rel=1e-6)
    assert settled.contracts == pytest.approx(10.0, rel=1e-6)


def test_trade_tracks_fill_quality_and_uses_actual_fill_price() -> None:
    trade = Trade(
        timestamp=datetime.now(UTC),
        market_id="m1",
        market_question="Will BTC close above 100k?",
        side=OutcomeSide.YES,
        entry_price=0.95,
        screened_price=0.95,
        position_size=4.85,
        fill_price=0.97,
        outcome=TradeOutcome.PENDING,
    )

    assert trade.screened_price == pytest.approx(0.95, rel=1e-6)
    assert trade.fill_slippage == pytest.approx(0.02, rel=1e-6)
    assert trade.fill_slippage_pct == pytest.approx(0.021053, rel=1e-6)
    assert trade.contracts == pytest.approx(5.0, rel=1e-6)


def test_trade_rejects_pending_with_pnl() -> None:
    with pytest.raises(ValueError, match="Pending trades"):
        Trade(
            timestamp=datetime.now(UTC),
            market_id="m1",
            market_question="Question",
            side=OutcomeSide.NO,
            entry_price=0.88,
            position_size=8.8,
            pnl=1.0,
        )


def test_order_validates_remaining_size() -> None:
    order = Order(
        created_at=datetime.now(UTC),
        market_id="m1",
        token_id="token-1",
        outcome_side=OutcomeSide.YES,
        action=OrderAction.BUY,
        price=0.91,
        size_usdc=25.0,
        status=OrderStatus.PARTIALLY_FILLED,
        filled_size_usdc=10.0,
    )

    assert order.is_open is True
    assert order.remaining_size_usdc == 15.0


def test_order_rejects_overfilled_state() -> None:
    with pytest.raises(ValueError, match="filled_size_usdc cannot exceed"):
        Order(
            created_at=datetime.now(UTC),
            market_id="m1",
            token_id="token-1",
            outcome_side=OutcomeSide.YES,
            action=OrderAction.BUY,
            price=0.91,
            size_usdc=5.0,
            filled_size_usdc=6.0,
        )


def test_bot_state_tracks_losses_and_recent_trade_window() -> None:
    loss_trade = Trade(
        timestamp=datetime.now(UTC) - timedelta(minutes=10),
        market_id="loss",
        market_question="Loss",
        side=OutcomeSide.YES,
        entry_price=0.9,
        position_size=9,
        outcome=TradeOutcome.LOSS,
        resolution_price=0.0,
        pnl=-9.0,
    )
    win_trade = Trade(
        timestamp=datetime.now(UTC) - timedelta(minutes=5),
        market_id="win",
        market_question="Win",
        side=OutcomeSide.NO,
        entry_price=0.8,
        position_size=8,
        outcome=TradeOutcome.WIN,
        resolution_price=1.0,
        pnl=2.0,
    )
    state = BotState(
        timestamp=datetime.now(UTC),
        bankroll=23.0,
        phase=1,
        bankroll_start_of_day=30.0,
        bankroll_start_of_week=35.0,
        daily_pnl=-7.0,
        weekly_pnl=-12.0,
        total_trades=2,
        recent_trades=(loss_trade, win_trade),
    )

    assert state.daily_loss == 7.0
    assert state.weekly_loss == 12.0
    assert state.get_recent_trades(1) == (win_trade,)
    assert state.win_rate(50) == 0.5


def test_bot_state_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        BotState(
            timestamp=datetime.now(),
            bankroll=10.0,
            phase=0,
            bankroll_start_of_day=10.0,
            bankroll_start_of_week=10.0,
        )
