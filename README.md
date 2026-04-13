# Polymarket Edge Bot

Python trading bot scaffold for late-market Polymarket execution, based on the architecture document at `/Users/st.dominic/Downloads/Polymarket_Bot_Architecture.docx`.

## Status

The repository now has a working supervised trading baseline:

- project scaffold, config, and schema
- validated market, trade, order, and state models
- Gamma market ingestion, scanner, ranker, and AI-side fallback rescoring
- SQLite trade/state tracking, dashboard rendering, and export utilities
- credential derivation, health checks, Polygon balance checks, and CLOB helpers
- paper trading, backtest simulation, and supervised live runners
- long-running daemon runner with reconciliation each cycle
- live preview, order submit/cancel, logging, and state sync
- WebSocket and Telegram integration primitives

The raw document is useful, but it is not production-ready as written. The main review findings are captured in [docs/architecture-review.md](/Users/st.dominic/poly/docs/architecture-review.md).

## Important Review Notes

- The document target date of July 31, 2025 is stale relative to April 6, 2026.
- Live trading code should not be enabled until paper trading, kill-switch tests, and credential validation are complete.
- The Polymarket CLOB integration should use the official `py-clob-client` rather than hand-built signed REST payloads where possible.
- Kill-switch thresholds are treated as code-level invariants, not user-editable runtime strategy knobs.

## Layout

```text
polymarket-bot/
├── .env.example
├── config.yaml
├── requirements.txt
├── README.md
├── api/
├── bot/
├── db/
├── docs/
├── models/
├── scripts/
└── tests/
```

## Quick Start

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
pytest
python -m bot.main
python scripts/derive_api_creds.py
python scripts/health_check.py
python scripts/sync_state.py
python scripts/show_dashboard.py
python scripts/supervised_live.py --cycles 1 --budget-usdc 3.0
python scripts/run_daemon.py --cycles 1 --budget-usdc 3.0
python scripts/reconcile_positions.py
```

The local machine currently defaults to Python 3.15, but the verified project runtime in this workspace is Python 3.14.

## Strategy Profiles

The default runtime profile in [`config.yaml`](/Users/st.dominic/poly/config.yaml) is now the literal Late-Market Edge strategy:

- `85c-97c`
- `2-6` hours to close
- minimum volume `$500`

The widened `0-30d` diagnostic configuration lives under `research_strategy` and is only intended for supply analysis.

Strict live/supervised runs use the default `strategy` profile:

```bash
python scripts/supervised_live.py --cycles 1 --budget-usdc 3.0
```

Research analysis uses the widened profile explicitly:

```bash
python scripts/analyze_candidate_pool.py --strategy-section research_strategy --top 10 --clusters 10
python scripts/analyze_candidate_pool.py --strategy-section strategy --top 10 --clusters 10
```
