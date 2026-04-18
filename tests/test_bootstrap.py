from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from bot.config import load_environment, load_runtime_config


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_config_loads() -> None:
    config = load_runtime_config(ROOT / "config.yaml")
    assert config.strategy.min_price == 0.85
    assert config.strategy.max_hours_to_close == 6.0
    assert config.strategy.min_hours_to_close == 2.0
    assert config.strategy.min_volume == 500.0
    assert config.strategy.min_score == 7.0
    assert config.strategy.sports_max_price == 0.97
    assert config.strategy.max_per_event_candidates == 2
    assert config.strategy.max_per_template_candidates == 3
    assert config.strategy.exact_score_max_per_event == 1
    assert config.strategy.paper_trade_minimum_trades == 200
    assert config.strategy.require_deterministic_markets is True
    assert config.strategy.min_liquidity == 2500.0
    assert config.strategy.max_volume_change_1h_pct == 15.0
    assert config.strategy.max_abs_one_hour_price_change == 0.03
    assert config.sizing.fractional_kelly == 1.0
    assert config.sizing.risk_per_trade_pct == 0.15
    assert config.sizing.max_position_pct == 0.15
    assert config.sizing.max_position_usd == 5.0
    assert config.sizing.min_valid_order_shares == 5.0
    assert config.sizing.bankroll_floor_for_live == 20.0
    assert config.execution.min_position_usd == 5.0
    assert config.execution.max_open_positions == 3
    assert config.execution.max_position_pct == 0.15
    assert config.execution.dynamic_min_position is True
    assert config.execution.execution_style == "maker"
    assert config.execution.exit_target_price == 0.99
    assert config.execution.websocket_heartbeat_seconds == 10


def test_runtime_config_loads_research_strategy() -> None:
    config = load_runtime_config(ROOT / "config.yaml", strategy_section="research_strategy")
    assert config.strategy.min_price == 0.85
    assert config.strategy.max_hours_to_close == 720.0
    assert config.strategy.min_hours_to_close == 0.0
    assert config.strategy.min_volume == 1000.0
    assert config.strategy.min_score == 7.25
    assert config.strategy.sports_max_price == 0.975
    assert config.strategy.require_deterministic_markets is True


def test_runtime_config_loads_btc_up_down_profile() -> None:
    config = load_runtime_config(ROOT / "config.yaml", strategy_section="btc_up_down")
    assert config.strategy.min_price == 0.4
    assert config.strategy.max_price == 0.6
    assert config.strategy.signal_mode == "momentum"
    assert config.strategy.execution_style == "taker"
    assert config.strategy.asset_keywords == ("bitcoin", "btc")
    assert config.strategy.spot_symbol == "XBTUSD"
    assert config.strategy.spot_min_abs_return_1h_pct == 0.01
    assert config.strategy.spot_min_abs_return_15m_pct == 0.003
    assert config.strategy.spot_min_contract_lag_pct == 0.005
    assert config.strategy.spot_max_age_seconds == 120
    assert config.strategy.catalyst_countries == ("united states",)
    assert "fomc" in config.strategy.catalyst_event_keywords
    assert config.strategy.catalyst_time_windows_utc == ()
    assert config.strategy.momentum_min_abs_volume_change_1h_pct == 15.0
    assert config.strategy.momentum_min_abs_one_hour_price_change == 0.015
    assert config.strategy.momentum_price_center == 0.5
    assert config.strategy.momentum_price_width == 0.1


def test_runtime_config_loads_hourly_momentum_multi_asset_profile() -> None:
    config = load_runtime_config(ROOT / "config.yaml", strategy_section="hourly_momentum_multi_asset")
    assert config.strategy.min_price == 0.4
    assert config.strategy.max_price == 0.6
    assert config.strategy.signal_mode == "momentum"
    assert config.strategy.execution_style == "taker"
    assert config.strategy.paper_trade_default is True
    assert config.strategy.risk_per_trade_pct == 0.015
    assert config.strategy.max_position_pct == 0.02
    assert config.strategy.max_position_usd == 3.0
    assert config.strategy.bankroll_floor_for_live == 20.0
    assert config.strategy.catalyst_mode == "soft_boost"
    assert config.strategy.catalyst_multiplier_weight == 0.18
    assert config.strategy.catalyst_multiplier_cap == 1.3
    assert config.strategy.spot_symbols == ("xbtusd", "ethusd", "solusd")
    assert config.strategy.spot_symbol == "XBTUSD"
    assert config.strategy.spot_max_age_seconds == 90
    assert config.strategy.momentum_min_abs_volume_change_1h_pct == 8.0
    assert config.strategy.momentum_min_abs_one_hour_price_change == 0.004
    assert config.strategy.momentum_price_center == 0.5
    assert config.strategy.momentum_price_width == 0.22


def test_environment_loads_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "PAPER_TRADE=false",
                "LOG_LEVEL=DEBUG",
                "DATABASE_URL=sqlite:///tmp.db",
                "POLY_WALLET_ADDRESS=0xabc",
                "POLY_SIGNATURE_TYPE=1",
                "POLY_CHAIN_ID=137",
                "POLY_CLOB_HOST=https://clob.polymarket.com",
            ]
        )
    )

    previous = dict(os.environ)
    try:
        for key in [
            "PAPER_TRADE",
            "LOG_LEVEL",
            "DATABASE_URL",
            "POLY_WALLET_ADDRESS",
            "POLY_SIGNATURE_TYPE",
            "POLY_CHAIN_ID",
            "POLY_CLOB_HOST",
        ]:
            os.environ.pop(key, None)
        env = load_environment(env_file)
    finally:
        os.environ.clear()
        os.environ.update(previous)

    assert env.paper_trade is False
    assert env.log_level == "DEBUG"
    assert env.database_url == "sqlite:///tmp.db"
    assert env.poly_wallet_address == "0xabc"
    assert env.poly_signature_type == 1
    assert env.poly_chain_id == 137
    assert env.poly_clob_host == "https://clob.polymarket.com"


def test_schema_creates_expected_tables() -> None:
    conn = sqlite3.connect(":memory:")
    schema = (ROOT / "db" / "schema.sql").read_text()
    conn.executescript(schema)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    }

    assert {"trades", "state", "corrections", "positions", "orders", "spot_ticks", "candidate_snapshots"} <= tables


def test_schema_includes_profile_columns() -> None:
    conn = sqlite3.connect(":memory:")
    schema = (ROOT / "db" / "schema.sql").read_text()
    conn.executescript(schema)

    def columns(table: str) -> set[str]:
        return {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    assert "strategy_name" in columns("trades")
    assert "strategy_name" in columns("state")
    assert "strategy_name" in columns("orders")
    assert "strategy_name" in columns("positions")
