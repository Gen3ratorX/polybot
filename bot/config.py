from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class FilterBounds:
    min_price: float
    max_price: float
    sports_max_price: float
    min_hours_to_close: float
    max_hours_to_close: float
    min_volume: float
    max_combined_price: float


@dataclass(frozen=True)
class FlowRiskThresholds:
    max_volume_change_1h_pct: float
    max_abs_one_hour_price_change: float
    min_liquidity: float


@dataclass(frozen=True)
class ExitRules:
    stop_loss_pct: float
    max_hold_minutes_without_progress: int


@dataclass(frozen=True)
class SignalRules:
    mode: str = "certainty"
    asset_keywords: tuple[str, ...] = ()
    catalyst_keywords: tuple[str, ...] = ()
    catalyst_mode: str = "hard"
    catalyst_multiplier_weight: float = 0.0
    catalyst_multiplier_cap: float = 1.0
    spot_symbols: tuple[str, ...] = ()
    gamma_tag_slugs: tuple[str, ...] = ()
    gamma_max_pages: int | None = None
    catalyst_time_windows_utc: tuple[str, ...] = ()
    catalyst_provider: str = "trading_economics"
    catalyst_countries: tuple[str, ...] = ()
    catalyst_event_keywords: tuple[str, ...] = ()
    catalyst_min_importance: int | None = None
    catalyst_arm_before_minutes: int = 30
    catalyst_arm_after_minutes: int = 45
    catalyst_refresh_minutes: int = 60
    catalyst_lookahead_days: int = 7
    spot_symbol: str = "XBTUSD"
    spot_min_abs_return_1h_pct: float | None = None
    spot_min_abs_return_15m_pct: float | None = None
    spot_min_contract_lag_pct: float | None = None
    spot_max_age_seconds: int | None = None
    momentum_min_abs_volume_change_1h_pct: float | None = None
    momentum_min_abs_one_hour_price_change: float | None = None
    momentum_price_center: float | None = None
    momentum_price_width: float | None = None


@dataclass(frozen=True)
class StrategyProfile:
    name: str
    filter_bounds: FilterBounds
    flow_risk_thresholds: FlowRiskThresholds
    category_weights: dict[str, float]
    exit_rules: ExitRules
    signal_rules: SignalRules | None
    min_score: float
    max_per_event_candidates: int
    max_per_template_candidates: int
    exact_score_max_per_event: int
    paper_trade_minimum_trades: int
    require_deterministic_markets: bool
    risk_per_trade_pct: float | None = None
    max_position_pct: float | None = None
    max_position_usd: float | None = None
    bankroll_floor_for_live: float | None = None
    paper_trade_default: bool = False
    execution_style: str | None = None

    @property
    def min_price(self) -> float:
        return self.filter_bounds.min_price

    @property
    def max_price(self) -> float:
        return self.filter_bounds.max_price

    @property
    def sports_max_price(self) -> float:
        return self.filter_bounds.sports_max_price

    @property
    def min_hours_to_close(self) -> float:
        return self.filter_bounds.min_hours_to_close

    @property
    def max_hours_to_close(self) -> float:
        return self.filter_bounds.max_hours_to_close

    @property
    def min_volume(self) -> float:
        return self.filter_bounds.min_volume

    @property
    def max_combined_price(self) -> float:
        return self.filter_bounds.max_combined_price

    @property
    def min_liquidity(self) -> float:
        return self.flow_risk_thresholds.min_liquidity

    @property
    def max_volume_change_1h_pct(self) -> float:
        return self.flow_risk_thresholds.max_volume_change_1h_pct

    @property
    def max_abs_one_hour_price_change(self) -> float:
        return self.flow_risk_thresholds.max_abs_one_hour_price_change

    @property
    def signal_mode(self) -> str:
        if self.signal_rules is None:
            return "certainty"
        return self.signal_rules.mode.strip().lower()

    @property
    def asset_keywords(self) -> tuple[str, ...]:
        if self.signal_rules is None:
            return ()
        return self.signal_rules.asset_keywords

    @property
    def catalyst_keywords(self) -> tuple[str, ...]:
        if self.signal_rules is None:
            return ()
        return self.signal_rules.catalyst_keywords

    @property
    def catalyst_mode(self) -> str:
        if self.signal_rules is None:
            return "hard"
        return self.signal_rules.catalyst_mode.strip().lower()

    @property
    def catalyst_multiplier_weight(self) -> float:
        if self.signal_rules is None:
            return 0.0
        return self.signal_rules.catalyst_multiplier_weight

    @property
    def catalyst_multiplier_cap(self) -> float:
        if self.signal_rules is None:
            return 1.0
        return self.signal_rules.catalyst_multiplier_cap

    @property
    def catalyst_time_windows_utc(self) -> tuple[str, ...]:
        if self.signal_rules is None:
            return ()
        return self.signal_rules.catalyst_time_windows_utc

    @property
    def catalyst_provider(self) -> str:
        if self.signal_rules is None:
            return "trading_economics"
        return self.signal_rules.catalyst_provider

    @property
    def catalyst_countries(self) -> tuple[str, ...]:
        if self.signal_rules is None:
            return ()
        return self.signal_rules.catalyst_countries

    @property
    def catalyst_event_keywords(self) -> tuple[str, ...]:
        if self.signal_rules is None:
            return ()
        return self.signal_rules.catalyst_event_keywords

    @property
    def catalyst_min_importance(self) -> int | None:
        if self.signal_rules is None:
            return None
        return self.signal_rules.catalyst_min_importance

    @property
    def catalyst_arm_before_minutes(self) -> int:
        if self.signal_rules is None:
            return 30
        return self.signal_rules.catalyst_arm_before_minutes

    @property
    def catalyst_arm_after_minutes(self) -> int:
        if self.signal_rules is None:
            return 45
        return self.signal_rules.catalyst_arm_after_minutes

    @property
    def catalyst_refresh_minutes(self) -> int:
        if self.signal_rules is None:
            return 60
        return self.signal_rules.catalyst_refresh_minutes

    @property
    def catalyst_lookahead_days(self) -> int:
        if self.signal_rules is None:
            return 7
        return self.signal_rules.catalyst_lookahead_days

    @property
    def spot_symbol(self) -> str:
        if self.signal_rules is None:
            return "XBTUSD"
        return self.signal_rules.spot_symbol

    @property
    def spot_symbols(self) -> tuple[str, ...]:
        if self.signal_rules is None:
            return ()
        return self.signal_rules.spot_symbols

    @property
    def gamma_tag_slugs(self) -> tuple[str, ...]:
        if self.signal_rules is None:
            return ()
        return self.signal_rules.gamma_tag_slugs

    @property
    def gamma_max_pages(self) -> int | None:
        if self.signal_rules is None:
            return None
        return self.signal_rules.gamma_max_pages

    @property
    def spot_min_abs_return_1h_pct(self) -> float | None:
        if self.signal_rules is None:
            return None
        return self.signal_rules.spot_min_abs_return_1h_pct

    @property
    def spot_min_abs_return_15m_pct(self) -> float | None:
        if self.signal_rules is None:
            return None
        return self.signal_rules.spot_min_abs_return_15m_pct

    @property
    def spot_min_contract_lag_pct(self) -> float | None:
        if self.signal_rules is None:
            return None
        return self.signal_rules.spot_min_contract_lag_pct

    @property
    def spot_max_age_seconds(self) -> int | None:
        if self.signal_rules is None:
            return None
        return self.signal_rules.spot_max_age_seconds

    @property
    def momentum_min_abs_volume_change_1h_pct(self) -> float | None:
        if self.signal_rules is None:
            return None
        return self.signal_rules.momentum_min_abs_volume_change_1h_pct

    @property
    def momentum_min_abs_one_hour_price_change(self) -> float | None:
        if self.signal_rules is None:
            return None
        return self.signal_rules.momentum_min_abs_one_hour_price_change

    @property
    def momentum_price_center(self) -> float | None:
        if self.signal_rules is None:
            return None
        return self.signal_rules.momentum_price_center

    @property
    def momentum_price_width(self) -> float | None:
        if self.signal_rules is None:
            return None
        return self.signal_rules.momentum_price_width


@dataclass(frozen=True)
class SizingConfig:
    fractional_kelly: float
    risk_per_trade_pct: float
    max_position_pct: float
    max_position_usd: float
    min_valid_order_shares: float
    bankroll_floor_for_live: float


@dataclass(frozen=True)
class ExecutionConfig:
    order_timeout_seconds: int
    max_open_positions: int
    max_position_pct: float
    min_position_usd: float
    max_position_usd: float
    dynamic_min_position: bool
    execution_style: str
    exit_target_price: float
    websocket_heartbeat_seconds: int


@dataclass(frozen=True)
class RiskConfig:
    operator_review_required_levels: list[str]
    health_check_interval_seconds: int


@dataclass(frozen=True)
class AutoCorrectConfig:
    cycle_trades: int
    loosen_threshold: float
    tighten_threshold: float
    warn_threshold: float


@dataclass(frozen=True)
class AIScoringConfig:
    enabled: bool
    min_rules_score_to_use_ai: float
    max_rules_score_to_use_ai: float
    max_daily_ai_calls: int


@dataclass(frozen=True)
class NotificationConfig:
    daily_summary_hour_utc: int


@dataclass(frozen=True)
class RuntimeConfig:
    sizing: SizingConfig
    strategy: StrategyProfile
    execution: ExecutionConfig
    risk: RiskConfig
    autocorrect: AutoCorrectConfig
    ai_scoring: AIScoringConfig
    notifications: NotificationConfig


@dataclass(frozen=True)
class EnvironmentConfig:
    poly_private_key: str | None
    poly_wallet_address: str | None
    poly_signature_type: int
    poly_chain_id: int
    poly_clob_host: str
    poly_api_key: str | None
    poly_api_secret: str | None
    poly_api_passphrase: str | None
    polygon_rpc_url: str | None
    polygon_rpc_backup: str | None
    anthropic_api_key: str | None
    telegram_token: str | None
    telegram_chat_id: str | None
    web_username: str | None
    web_password: str | None
    web_bind_host: str
    web_bind_port: int
    trading_economics_credentials: str | None
    news_api_key: str | None
    odds_api_key: str | None
    paper_trade: bool
    log_level: str
    database_url: str

    @classmethod
    def from_env(cls) -> "EnvironmentConfig":
        return cls(
            poly_private_key=os.getenv("POLY_PRIVATE_KEY"),
            poly_wallet_address=os.getenv("POLY_WALLET_ADDRESS"),
            poly_signature_type=_read_int("POLY_SIGNATURE_TYPE", default=1),
            poly_chain_id=_read_int("POLY_CHAIN_ID", default=137),
            poly_clob_host=os.getenv("POLY_CLOB_HOST", "https://clob.polymarket.com"),
            poly_api_key=os.getenv("POLY_API_KEY"),
            poly_api_secret=os.getenv("POLY_API_SECRET"),
            poly_api_passphrase=os.getenv("POLY_API_PASSPHRASE"),
            polygon_rpc_url=os.getenv("POLYGON_RPC_URL"),
            polygon_rpc_backup=os.getenv("POLYGON_RPC_BACKUP"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            telegram_token=os.getenv("TELEGRAM_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            web_username=os.getenv("WEB_USERNAME"),
            web_password=os.getenv("WEB_PASSWORD"),
            web_bind_host=os.getenv("WEB_BIND_HOST", "0.0.0.0"),
            web_bind_port=_read_int("WEB_BIND_PORT", default=8080),
            trading_economics_credentials=os.getenv("TRADING_ECONOMICS_CREDENTIALS"),
            news_api_key=os.getenv("NEWS_API_KEY"),
            odds_api_key=os.getenv("ODDS_API_KEY"),
            paper_trade=_read_bool("PAPER_TRADE", default=True),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            database_url=os.getenv("DATABASE_URL", "sqlite:///bot.db"),
        )


def load_environment(env_path: str | Path | None = None) -> EnvironmentConfig:
    if env_path is not None:
        load_dotenv(Path(env_path), override=False)
    else:
        load_dotenv(override=False)
    return EnvironmentConfig.from_env()


def load_runtime_config(
    config_path: str | Path = "config.yaml",
    *,
    strategy_section: str = "late_market_edge",
) -> RuntimeConfig:
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text()) or {}

    return RuntimeConfig(
        sizing=_load_sizing(raw["sizing"]),
        strategy=_load_strategy(_resolve_strategy_block(raw, strategy_section), strategy_section),
        execution=_load_execution(raw["execution"]),
        risk=_load_risk(raw["risk"]),
        autocorrect=_load_autocorrect(raw["autocorrect"]),
        ai_scoring=_load_ai_scoring(raw["ai_scoring"]),
        notifications=_load_notifications(raw["notifications"]),
    )


def _resolve_strategy_block(raw: dict[str, Any], strategy_section: str) -> dict[str, Any]:
    profiles = raw.get("profiles")
    if isinstance(profiles, dict):
        aliases = {
            "strategy": "late_market_edge",
            "late_market_edge": "late_market_edge",
            "research_strategy": "research_strategy",
            "btc": "btc_up_down",
            "btc_up_down": "btc_up_down",
            "btc_event_volatility": "btc_up_down",
            "btc_momentum_scalp": "hourly_momentum_multi_asset",
            "hourly_momentum_multi_asset": "hourly_momentum_multi_asset",
        }
        profile_name = aliases.get(strategy_section, strategy_section)
        profile = profiles.get(profile_name)
        if isinstance(profile, dict):
            return profile
    section = raw.get(strategy_section)
    if isinstance(section, dict):
        return section
    raise KeyError(f"Strategy profile not found: {strategy_section}")


def _load_strategy(data: dict[str, Any], name: str) -> StrategyProfile:
    if "filter_bounds" in data:
        return StrategyProfile(
            name=name,
            filter_bounds=FilterBounds(**data["filter_bounds"]),
            flow_risk_thresholds=FlowRiskThresholds(**data["flow_risk_thresholds"]),
            category_weights=dict(data.get("category_weights", {})),
            exit_rules=ExitRules(**data["exit_rules"]),
            signal_rules=_load_signal_rules(data.get("signal_rules")),
            min_score=float(data["min_score"]),
            max_per_event_candidates=int(data["max_per_event_candidates"]),
            max_per_template_candidates=int(data["max_per_template_candidates"]),
            exact_score_max_per_event=int(data["exact_score_max_per_event"]),
            paper_trade_minimum_trades=int(data["paper_trade_minimum_trades"]),
            require_deterministic_markets=bool(data["require_deterministic_markets"]),
            risk_per_trade_pct=_optional_float(data.get("risk_per_trade_pct")),
            max_position_pct=_optional_float(data.get("max_position_pct")),
            max_position_usd=_optional_float(data.get("max_position_usd")),
            bankroll_floor_for_live=_optional_float(data.get("bankroll_floor_for_live")),
            paper_trade_default=bool(data.get("paper_trade_default", False)),
            execution_style=_optional_text(data.get("execution_style")),
        )
    return StrategyProfile(
        name=name,
        filter_bounds=FilterBounds(
            min_price=float(data["min_price"]),
            max_price=float(data["max_price"]),
            sports_max_price=float(data["sports_max_price"]),
            min_hours_to_close=float(data["min_hours_to_close"]),
            max_hours_to_close=float(data["max_hours_to_close"]),
            min_volume=float(data["min_volume"]),
            max_combined_price=float(data["max_combined_price"]),
        ),
        flow_risk_thresholds=FlowRiskThresholds(
            max_volume_change_1h_pct=float(data["max_volume_change_1h_pct"]),
            max_abs_one_hour_price_change=float(data["max_abs_one_hour_price_change"]),
            min_liquidity=float(data["min_liquidity"]),
        ),
        category_weights=dict(
            data.get(
                "category_weights",
                {
                    "sports": 1.0,
                    "crypto": 0.85,
                    "politics": 0.75,
                    "unknown": 0.80,
                },
            )
        ),
        exit_rules=ExitRules(
            stop_loss_pct=float(data.get("stop_loss_pct", 0.05)),
            max_hold_minutes_without_progress=int(data.get("max_hold_minutes_without_progress", 180)),
        ),
        signal_rules=_load_signal_rules(data.get("signal_rules")),
        min_score=float(data["min_score"]),
        max_per_event_candidates=int(data["max_per_event_candidates"]),
        max_per_template_candidates=int(data["max_per_template_candidates"]),
        exact_score_max_per_event=int(data["exact_score_max_per_event"]),
        paper_trade_minimum_trades=int(data["paper_trade_minimum_trades"]),
        require_deterministic_markets=bool(data["require_deterministic_markets"]),
        risk_per_trade_pct=_optional_float(data.get("risk_per_trade_pct")),
        max_position_pct=_optional_float(data.get("max_position_pct")),
        max_position_usd=_optional_float(data.get("max_position_usd")),
        bankroll_floor_for_live=_optional_float(data.get("bankroll_floor_for_live")),
        paper_trade_default=bool(data.get("paper_trade_default", False)),
        execution_style=_optional_text(data.get("execution_style")),
    )


def _load_sizing(data: dict[str, Any]) -> SizingConfig:
    return SizingConfig(**data)


def _load_execution(data: dict[str, Any]) -> ExecutionConfig:
    return ExecutionConfig(**data)


def _load_risk(data: dict[str, Any]) -> RiskConfig:
    return RiskConfig(**data)


def _load_autocorrect(data: dict[str, Any]) -> AutoCorrectConfig:
    return AutoCorrectConfig(**data)


def _load_ai_scoring(data: dict[str, Any]) -> AIScoringConfig:
    return AIScoringConfig(**data)


def _load_notifications(data: dict[str, Any]) -> NotificationConfig:
    return NotificationConfig(**data)


def _load_signal_rules(data: Any) -> SignalRules | None:
    if not isinstance(data, dict):
        return None
    return SignalRules(
        mode=str(data.get("mode", "certainty")),
        asset_keywords=_to_str_tuple(data.get("asset_keywords")),
        catalyst_keywords=_to_str_tuple(data.get("catalyst_keywords")),
        catalyst_mode=str(data.get("catalyst_mode", "hard")),
        catalyst_multiplier_weight=float(data.get("catalyst_multiplier_weight", 0.0)),
        catalyst_multiplier_cap=float(data.get("catalyst_multiplier_cap", 1.0)),
        spot_symbols=_to_str_tuple(data.get("spot_symbols")),
        gamma_tag_slugs=_to_str_tuple(data.get("gamma_tag_slugs")),
        gamma_max_pages=_optional_int(data.get("gamma_max_pages")),
        catalyst_time_windows_utc=_to_str_tuple(data.get("catalyst_time_windows_utc")),
        catalyst_provider=str(data.get("catalyst_provider", "trading_economics")),
        catalyst_countries=_to_str_tuple(data.get("catalyst_countries")),
        catalyst_event_keywords=_to_str_tuple(data.get("catalyst_event_keywords")),
        catalyst_min_importance=_optional_int(data.get("catalyst_min_importance")),
        catalyst_arm_before_minutes=int(data.get("catalyst_arm_before_minutes", 30)),
        catalyst_arm_after_minutes=int(data.get("catalyst_arm_after_minutes", 45)),
        catalyst_refresh_minutes=int(data.get("catalyst_refresh_minutes", 60)),
        catalyst_lookahead_days=int(data.get("catalyst_lookahead_days", 7)),
        spot_symbol=str(data.get("spot_symbol", "XBTUSD")),
        spot_min_abs_return_1h_pct=_optional_float(data.get("spot_min_abs_return_1h_pct")),
        spot_min_abs_return_15m_pct=_optional_float(data.get("spot_min_abs_return_15m_pct")),
        spot_min_contract_lag_pct=_optional_float(data.get("spot_min_contract_lag_pct")),
        spot_max_age_seconds=_optional_int(data.get("spot_max_age_seconds")),
        momentum_min_abs_volume_change_1h_pct=_optional_float(data.get("momentum_min_abs_volume_change_1h_pct")),
        momentum_min_abs_one_hour_price_change=_optional_float(data.get("momentum_min_abs_one_hour_price_change")),
        momentum_price_center=_optional_float(data.get("momentum_price_center")),
        momentum_price_width=_optional_float(data.get("momentum_price_width")),
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Expected optional text field to be a string")
    text = value.strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _to_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip().lower() for item in value if str(item).strip())
    if isinstance(value, str):
        text = value.strip()
        return (text.lower(),) if text else ()
    raise ValueError("Expected a list of strings")


def _read_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_int(name: str, *, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def resolve_execution_style(runtime: RuntimeConfig) -> str:
    return (runtime.strategy.execution_style or runtime.execution.execution_style).strip().lower()


StrategyConfig = StrategyProfile
