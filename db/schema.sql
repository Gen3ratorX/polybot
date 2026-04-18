PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS trades (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  session_id        TEXT,
  strategy_name     TEXT,
  market_id         TEXT NOT NULL,
  market_question   TEXT NOT NULL,
  category          TEXT,
  side              TEXT NOT NULL CHECK (side IN ('YES', 'NO')),
  entry_price       REAL NOT NULL,
  screened_price    REAL,
  position_size     REAL NOT NULL,
  score             REAL,
  hours_to_close    REAL,
  order_id          TEXT,
  fill_price        REAL,
  fill_slippage     REAL,
  fill_slippage_pct REAL,
  fill_time         DATETIME,
  outcome           TEXT CHECK (outcome IN ('WIN', 'LOSS', 'PENDING', 'CANCELLED')),
  resolution_price  REAL,
  pnl               REAL,
  execution_fee     REAL DEFAULT 0,
  notes             TEXT,
  paper_trade       BOOLEAN NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS state (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  session_id              TEXT,
  strategy_name           TEXT,
  bankroll                REAL NOT NULL,
  phase                   INTEGER NOT NULL,
  bankroll_start_of_day   REAL NOT NULL DEFAULT 0,
  bankroll_start_of_week  REAL NOT NULL DEFAULT 0,
  daily_pnl               REAL NOT NULL DEFAULT 0,
  weekly_pnl              REAL NOT NULL DEFAULT 0,
  total_trades            INTEGER NOT NULL DEFAULT 0,
  consecutive_failures    INTEGER NOT NULL DEFAULT 0,
  open_orders             INTEGER NOT NULL DEFAULT 0,
  open_positions          INTEGER NOT NULL DEFAULT 0,
  win_rate_50             REAL,
  win_rate_100            REAL,
  win_rate_all            REAL,
  strategy_min_price      REAL NOT NULL DEFAULT 0.85,
  strategy_min_score      REAL NOT NULL DEFAULT 7.0,
  is_paused               BOOLEAN NOT NULL DEFAULT 0,
  pause_level             TEXT,
  pause_reason            TEXT,
  pause_until             DATETIME
);

CREATE TABLE IF NOT EXISTS corrections (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  trigger_win_rate  REAL NOT NULL,
  action            TEXT NOT NULL,
  old_min_price     REAL,
  new_min_price     REAL,
  old_min_score     REAL,
  new_min_score     REAL,
  notes             TEXT
);

CREATE TABLE IF NOT EXISTS orders (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  session_id          TEXT,
  order_id            TEXT NOT NULL UNIQUE,
  strategy_name       TEXT,
  market_id           TEXT NOT NULL,
  status              TEXT NOT NULL,
  requested_size      REAL NOT NULL,
  limit_price         REAL,
  filled_size         REAL NOT NULL DEFAULT 0,
  last_seen_status    TEXT,
  last_seen_at        DATETIME,
  exchange_payload    TEXT
);

CREATE TABLE IF NOT EXISTS catalyst_events (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  provider          TEXT NOT NULL,
  calendar_id       TEXT NOT NULL,
  country           TEXT NOT NULL,
  category          TEXT NOT NULL,
  event             TEXT NOT NULL,
  event_time        DATETIME NOT NULL,
  importance        INTEGER NOT NULL,
  source            TEXT,
  source_url        TEXT,
  url               TEXT,
  reference         TEXT,
  reference_date    DATETIME,
  actual            TEXT,
  previous          TEXT,
  forecast          TEXT,
  te_forecast       TEXT,
  ticker            TEXT,
  symbol            TEXT,
  currency          TEXT,
  unit              TEXT,
  last_update       DATETIME,
  raw_payload       TEXT,
  UNIQUE(provider, calendar_id)
);

CREATE TABLE IF NOT EXISTS spot_ticks (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  symbol            TEXT NOT NULL,
  pair              TEXT NOT NULL,
  source            TEXT NOT NULL,
  fetch_latency_seconds REAL,
  observed_at       DATETIME NOT NULL,
  spot_price        REAL NOT NULL,
  return_15m_pct    REAL,
  return_1h_pct     REAL,
  age_seconds       REAL NOT NULL DEFAULT 0,
  raw_payload       TEXT
);

CREATE TABLE IF NOT EXISTS positions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  session_id        TEXT,
  strategy_name     TEXT,
  market_id         TEXT NOT NULL,
  market_question   TEXT NOT NULL,
  category          TEXT,
  side              TEXT NOT NULL CHECK (side IN ('YES', 'NO')),
  fill_price        REAL NOT NULL,
  shares            REAL NOT NULL,
  cost_basis        REAL NOT NULL,
  order_id          TEXT UNIQUE,
  status            TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
  resolved_at       DATETIME,
  resolution_price  REAL,
  pnl               REAL,
  paper_trade       BOOLEAN NOT NULL DEFAULT 0,
  notes             TEXT
);

CREATE TABLE IF NOT EXISTS profile_controls (
  control_key         TEXT PRIMARY KEY,
  scope               TEXT NOT NULL CHECK (scope IN ('GLOBAL', 'PROFILE')),
  profile_name        TEXT,
  desired_state       TEXT NOT NULL CHECK (desired_state IN ('RUNNING', 'PAUSED', 'STOPPED')),
  run_once_pending    INTEGER NOT NULL DEFAULT 0,
  updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by          TEXT,
  source_chat_id      TEXT,
  source_message_id   INTEGER,
  last_command        TEXT,
  notes               TEXT
);

CREATE TABLE IF NOT EXISTS telegram_router_state (
  id                  INTEGER PRIMARY KEY CHECK (id = 1),
  last_update_id      INTEGER NOT NULL DEFAULT 0,
  updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_error          TEXT
);

CREATE TABLE IF NOT EXISTS candidate_snapshots (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  market_id             TEXT NOT NULL,
  event_id              TEXT,
  event_slug            TEXT,
  event_title           TEXT,
  cluster_key           TEXT NOT NULL,
  template_key          TEXT NOT NULL,
  market_question       TEXT NOT NULL,
  category              TEXT,
  score                 REAL,
  final_score           REAL,
  selected_side         TEXT CHECK (selected_side IN ('YES', 'NO')),
  selected_price        REAL,
  hours_to_close        REAL,
  volume                REAL,
  selected              BOOLEAN NOT NULL DEFAULT 0,
  notes                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_session_id ON trades(session_id);
CREATE INDEX IF NOT EXISTS idx_trades_strategy_name ON trades(strategy_name);
CREATE INDEX IF NOT EXISTS idx_trades_category ON trades(category);
CREATE INDEX IF NOT EXISTS idx_trades_outcome ON trades(outcome);
CREATE INDEX IF NOT EXISTS idx_state_timestamp ON state(timestamp);
CREATE INDEX IF NOT EXISTS idx_state_session_id ON state(session_id);
CREATE INDEX IF NOT EXISTS idx_state_strategy_name ON state(strategy_name);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_session_id ON orders(session_id);
CREATE INDEX IF NOT EXISTS idx_orders_strategy_name ON orders(strategy_name);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id);
CREATE INDEX IF NOT EXISTS idx_catalyst_events_provider ON catalyst_events(provider);
CREATE INDEX IF NOT EXISTS idx_catalyst_events_time ON catalyst_events(event_time);
CREATE INDEX IF NOT EXISTS idx_catalyst_events_country ON catalyst_events(country);
CREATE INDEX IF NOT EXISTS idx_catalyst_events_event ON catalyst_events(event);
CREATE INDEX IF NOT EXISTS idx_spot_ticks_symbol ON spot_ticks(symbol);
CREATE INDEX IF NOT EXISTS idx_spot_ticks_observed_at ON spot_ticks(observed_at);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_session_id ON positions(session_id);
CREATE INDEX IF NOT EXISTS idx_positions_strategy_name ON positions(strategy_name);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_profile_controls_scope ON profile_controls(scope);
CREATE INDEX IF NOT EXISTS idx_profile_controls_profile_name ON profile_controls(profile_name);
CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_timestamp ON candidate_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_category ON candidate_snapshots(category);
CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_cluster_key ON candidate_snapshots(cluster_key);
CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_template_key ON candidate_snapshots(template_key);
