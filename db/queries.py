"""Central location for SQL query helpers."""

CREATE_SCHEMA = "db/schema.sql"

INSERT_SPOT_TICK = """
INSERT INTO spot_ticks (
  timestamp,
  symbol,
  pair,
  source,
  fetch_latency_seconds,
  observed_at,
  spot_price,
  return_15m_pct,
  return_1h_pct,
  age_seconds,
  raw_payload
)
VALUES (
  :timestamp,
  :symbol,
  :pair,
  :source,
  :fetch_latency_seconds,
  :observed_at,
  :spot_price,
  :return_15m_pct,
  :return_1h_pct,
  :age_seconds,
  :raw_payload
)
"""

INSERT_CATALYST_EVENT = """
INSERT INTO catalyst_events (
  timestamp,
  provider,
  calendar_id,
  country,
  category,
  event,
  event_time,
  importance,
  source,
  source_url,
  url,
  reference,
  reference_date,
  actual,
  previous,
  forecast,
  te_forecast,
  ticker,
  symbol,
  currency,
  unit,
  last_update,
  raw_payload
)
VALUES (
  :timestamp,
  :provider,
  :calendar_id,
  :country,
  :category,
  :event,
  :event_time,
  :importance,
  :source,
  :source_url,
  :url,
  :reference,
  :reference_date,
  :actual,
  :previous,
  :forecast,
  :te_forecast,
  :ticker,
  :symbol,
  :currency,
  :unit,
  :last_update,
  :raw_payload
)
ON CONFLICT(provider, calendar_id) DO UPDATE SET
  timestamp=excluded.timestamp,
  country=excluded.country,
  category=excluded.category,
  event=excluded.event,
  event_time=excluded.event_time,
  importance=excluded.importance,
  source=excluded.source,
  source_url=excluded.source_url,
  url=excluded.url,
  reference=excluded.reference,
  reference_date=excluded.reference_date,
  actual=excluded.actual,
  previous=excluded.previous,
  forecast=excluded.forecast,
  te_forecast=excluded.te_forecast,
  ticker=excluded.ticker,
  symbol=excluded.symbol,
  currency=excluded.currency,
  unit=excluded.unit,
  last_update=excluded.last_update,
  raw_payload=excluded.raw_payload
"""

INSERT_TRADE = """
INSERT INTO trades (
  timestamp,
  strategy_name,
  market_id,
  market_question,
  category,
  side,
  entry_price,
  screened_price,
  position_size,
  score,
  hours_to_close,
  order_id,
  fill_price,
  fill_slippage,
  fill_slippage_pct,
  fill_time,
  outcome,
  resolution_price,
  pnl,
  execution_fee,
  notes,
  paper_trade
)
VALUES (
  :timestamp,
  :strategy_name,
  :market_id,
  :market_question,
  :category,
  :side,
  :entry_price,
  :screened_price,
  :position_size,
  :score,
  :hours_to_close,
  :order_id,
  :fill_price,
  :fill_slippage,
  :fill_slippage_pct,
  :fill_time,
  :outcome,
  :resolution_price,
  :pnl,
  :execution_fee,
  :notes,
  :paper_trade
)
"""

INSERT_STATE = """
INSERT INTO state (
  timestamp,
  strategy_name,
  bankroll,
  phase,
  bankroll_start_of_day,
  bankroll_start_of_week,
  daily_pnl,
  weekly_pnl,
  total_trades,
  consecutive_failures,
  open_orders,
  open_positions,
  win_rate_50,
  win_rate_100,
  win_rate_all,
  strategy_min_price,
  strategy_min_score,
  is_paused,
  pause_level,
  pause_reason,
  pause_until
)
VALUES (
  :timestamp,
  :strategy_name,
  :bankroll,
  :phase,
  :bankroll_start_of_day,
  :bankroll_start_of_week,
  :daily_pnl,
  :weekly_pnl,
  :total_trades,
  :consecutive_failures,
  :open_orders,
  :open_positions,
  :win_rate_50,
  :win_rate_100,
  :win_rate_all,
  :strategy_min_price,
  :strategy_min_score,
  :is_paused,
  :pause_level,
  :pause_reason,
  :pause_until
)
"""

UPSERT_POSITION = """
INSERT INTO positions (
  id,
  timestamp,
  strategy_name,
  market_id,
  market_question,
  category,
  side,
  fill_price,
  shares,
  cost_basis,
  order_id,
  status,
  resolved_at,
  resolution_price,
  pnl,
  paper_trade,
  notes
)
VALUES (
  :id,
  :timestamp,
  :strategy_name,
  :market_id,
  :market_question,
  :category,
  :side,
  :fill_price,
  :shares,
  :cost_basis,
  :order_id,
  :status,
  :resolved_at,
  :resolution_price,
  :pnl,
  :paper_trade,
  :notes
)
ON CONFLICT(order_id) DO UPDATE SET
  timestamp=excluded.timestamp,
  strategy_name=excluded.strategy_name,
  market_id=excluded.market_id,
  market_question=excluded.market_question,
  category=excluded.category,
  side=excluded.side,
  fill_price=excluded.fill_price,
  shares=excluded.shares,
  cost_basis=excluded.cost_basis,
  status=excluded.status,
  resolved_at=excluded.resolved_at,
  resolution_price=excluded.resolution_price,
  pnl=excluded.pnl,
  paper_trade=excluded.paper_trade,
  notes=excluded.notes
"""

INSERT_CANDIDATE_SNAPSHOT = """
INSERT INTO candidate_snapshots (
  timestamp,
  market_id,
  event_id,
  event_slug,
  event_title,
  cluster_key,
  template_key,
  market_question,
  category,
  score,
  final_score,
  selected_side,
  selected_price,
  hours_to_close,
  volume,
  selected,
  notes
)
VALUES (
  :timestamp,
  :market_id,
  :event_id,
  :event_slug,
  :event_title,
  :cluster_key,
  :template_key,
  :market_question,
  :category,
  :score,
  :final_score,
  :selected_side,
  :selected_price,
  :hours_to_close,
  :volume,
  :selected,
  :notes
)
"""

SELECT_RECENT_TRADES = """
SELECT
  timestamp,
  strategy_name,
  market_id,
  market_question,
  category,
  side,
  entry_price,
  screened_price,
  position_size,
  score,
  hours_to_close,
  order_id,
  fill_price,
  fill_slippage,
  fill_slippage_pct,
  fill_time,
  outcome,
  resolution_price,
  pnl,
  execution_fee,
  paper_trade
FROM trades
ORDER BY timestamp DESC, id DESC
LIMIT :limit
"""

SELECT_RECENT_CATALYST_EVENTS = """
SELECT
  timestamp,
  provider,
  calendar_id,
  country,
  category,
  event,
  event_time,
  importance,
  source,
  source_url,
  url,
  reference,
  reference_date,
  actual,
  previous,
  forecast,
  te_forecast,
  ticker,
  symbol,
  currency,
  unit,
  last_update,
  raw_payload
FROM catalyst_events
WHERE provider = :provider
ORDER BY event_time DESC, id DESC
LIMIT :limit
"""

SELECT_LATEST_STATE = """
SELECT
  timestamp,
  strategy_name,
  bankroll,
  phase,
  bankroll_start_of_day,
  bankroll_start_of_week,
  daily_pnl,
  weekly_pnl,
  total_trades,
  consecutive_failures,
  open_orders,
  open_positions,
  strategy_min_price,
  strategy_min_score,
  is_paused,
  pause_level,
  pause_reason,
  pause_until
FROM state
ORDER BY timestamp DESC, id DESC
LIMIT 1
"""

SELECT_LATEST_SPOT_TICK = """
SELECT
  timestamp,
  symbol,
  pair,
  source,
  fetch_latency_seconds,
  observed_at,
  spot_price,
  return_15m_pct,
  return_1h_pct,
  age_seconds,
  raw_payload
FROM spot_ticks
WHERE symbol = :symbol
ORDER BY observed_at DESC, id DESC
LIMIT 1
"""

SELECT_RECENT_SPOT_TICKS = """
SELECT
  timestamp,
  symbol,
  pair,
  source,
  fetch_latency_seconds,
  observed_at,
  spot_price,
  return_15m_pct,
  return_1h_pct,
  age_seconds,
  raw_payload
FROM spot_ticks
WHERE symbol = :symbol
ORDER BY observed_at DESC, id DESC
LIMIT :limit
"""

SELECT_RESOLVED_TRADES = """
SELECT pnl
FROM trades
WHERE pnl IS NOT NULL
ORDER BY timestamp DESC, id DESC
LIMIT :limit
"""

SELECT_OPEN_POSITIONS = """
SELECT
  id,
  timestamp,
  strategy_name,
  market_id,
  market_question,
  category,
  side,
  fill_price,
  shares,
  cost_basis,
  order_id,
  status,
  resolved_at,
  resolution_price,
  pnl,
  paper_trade,
  notes
FROM positions
WHERE status = 'OPEN'
ORDER BY timestamp ASC, id ASC
LIMIT :limit
"""

SELECT_RECENT_POSITIONS = """
SELECT
  id,
  timestamp,
  strategy_name,
  market_id,
  market_question,
  category,
  side,
  fill_price,
  shares,
  cost_basis,
  order_id,
  status,
  resolved_at,
  resolution_price,
  pnl,
  paper_trade,
  notes
FROM positions
ORDER BY timestamp DESC, id DESC
LIMIT :limit
"""

SELECT_POSITION_BY_ORDER_ID = """
SELECT
  id,
  timestamp,
  strategy_name,
  market_id,
  market_question,
  category,
  side,
  fill_price,
  shares,
  cost_basis,
  order_id,
  status,
  resolved_at,
  resolution_price,
  pnl,
  paper_trade,
  notes
FROM positions
WHERE order_id = :order_id
LIMIT 1
"""
