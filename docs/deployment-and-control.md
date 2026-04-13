# Deployment and Control

This document describes how to run Polybot in production and how to control it.
It does not cover strategy internals.

## Services

The Docker Compose deployment runs two services:

- `polybot`
  - trading daemon
  - executes supervised cycles
  - handles reconciliation, entries, exits, and state snapshots

- `telegram-router`
  - polls Telegram for commands
  - writes durable control state to SQLite
  - routes global and per-profile commands

Both services share:

- `config.yaml`
- `.env`
- a persistent SQLite volume

## Runtime Files

The bot expects these files at runtime:

- `.env`
  - API keys
  - wallet credentials
  - Telegram credentials
  - database URL

- `config.yaml`
  - runtime profile configuration
  - sizing
  - execution style
  - control thresholds

## Docker Commands

Build and start:

```bash
docker compose up -d --build --pull never
```

Check status:

```bash
docker compose ps
```

Follow daemon logs:

```bash
docker compose logs -f polybot
```

Follow Telegram router logs:

```bash
docker compose logs -f telegram-router
```

Stop the stack:

```bash
docker compose down
```

## Telegram Control

Global commands:

- `/status`
- `/pause`
- `/resume`
- `/stop_all`

Per-profile commands:

- `/status late_market_edge`
- `/pause late_market_edge`
- `/resume late_market_edge`
- `/run_once late_market_edge`
- `/start late_market_edge`
- `/stop late_market_edge`
- `/status btc_up_down`
- `/pause btc_up_down`
- `/resume btc_up_down`
- `/run_once btc_up_down`
- `/start btc_up_down`
- `/stop btc_up_down`

Command semantics:

- `status`
  - reports profile or global control state
- `pause`
  - pauses the selected profile or all profiles
- `resume`
  - resumes the selected profile or all profiles
- `run_once`
  - queues one supervised cycle for the selected profile
- `start`
  - resumes the selected profile
- `stop`
  - stops the selected profile
- `stop_all`
  - stops the whole bot cleanly

## Persistent State

SQLite stores:

- trades
- orders
- positions
- state snapshots
- catalyst events
- spot ticks
- profile control state
- Telegram router cursor state

This allows the bot to recover across restarts without losing command history or runtime state.

## Production Expectations

Before treating the stack as live:

- confirm both containers are running
- confirm the Telegram router is polling
- confirm `bot.main --json` shows the right active profile
- confirm the SQLite volume survives a restart
- confirm Telegram commands update control state as expected

## First Boot Checklist

Use this checklist before you call the deployment ready:

1. The VPS boots and accepts SSH with the deployed key.
2. Docker and Docker Compose are installed.
3. The private repo is cloned on the server.
4. `.env` is present and contains the live credentials.
5. `config.yaml` is present and points to the intended profiles.
6. The SQLite volume is mounted and writeable.
7. `docker compose up -d --build --pull never` completes successfully.
8. `docker compose ps` shows both services as `Up`.
9. `docker compose logs -f polybot` shows no startup traceback.
10. `docker compose logs -f telegram-router` shows polling activity.
11. `docker compose exec polybot python -m bot.main --json` reports the expected status.

## Recovery and Restart

The Compose services are configured with `restart: unless-stopped`, so they should restart automatically after a crash or host reboot.

If you need to intervene:

- restart the daemon only:
```bash
docker compose restart polybot
```

- restart the router only:
```bash
docker compose restart telegram-router
```

- rebuild both services:
```bash
docker compose up -d --build --pull never
```

If the daemon comes back but the router does not, check the router logs first. The command plane can fail independently from execution.

## Backup and Restore

The deployment has three critical data items:

- `config.yaml`
- `.env`
- the SQLite database stored on the Docker volume

Backup expectations:

- snapshot the database before any major upgrade
- store the environment file and config file with the same backup set
- keep backups outside the VPS

Restore order:

1. restore `.env`
2. restore `config.yaml`
3. restore the SQLite database volume or file
4. start Compose

If the SQLite state is lost, the bot will lose:

- trade history
- orders
- positions
- state snapshots
- profile control state
- Telegram router cursor state

## Failure Modes

Known failure classes and the right interpretation:

- daemon tracebacks on startup
  - usually config, environment, or dependency issues

- router stops polling
  - control commands will stop updating, but the daemon can keep running

- profile shows paused or stopped unexpectedly
  - likely a Telegram command or risk-manager pause

- bankroll depletion pause
  - the risk layer is intentionally halting the profile

- open order persists without a fill
  - the order is resting, not executed

- profile accounting looks wrong after restart
  - check whether the same database volume was mounted and whether the latest state was restored

## Monitoring

Minimum monitoring loop:

- daemon logs
- router logs
- `bot.main --json`
- `docker compose ps`

The key values to watch are:

- bankroll
- open orders
- open positions
- pause status
- router cursor health
- profile-specific trade count and PnL

## Manual Control Guidance

Use Telegram for normal control actions:

- pause a single profile
- resume a single profile
- run one cycle for a single profile
- stop the whole bot only when you need a hard shutdown

Use Docker only for infrastructure-level intervention:

- container restart
- rebuild
- log inspection

Do not delete the SQLite volume unless you intentionally want a fresh ledger.

## Notes

This repository separates trading logic from control logic so deployment can be managed independently of strategy changes.
