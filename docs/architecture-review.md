# Architecture Review

Review completed against `/Users/st.dominic/Downloads/Polymarket_Bot_Architecture.docx` on April 6, 2026.

## High-confidence findings

1. The timeline is stale.
The document target date is July 31, 2025, which is already in the past as of April 6, 2026. The build should treat that date as a historical target, not an executable requirement.

2. The strategy has an unresolved truth-source gap.
The document repeatedly says the bot trades markets where the outcome is "effectively known", but it does not define a deterministic verification layer for sports, crypto, politics, or disputes. A production system needs category-specific resolution checks before placing orders.

3. The scanner and ranker field assumptions are partly mismatched with current market-data docs.
The document relies on `volumeNum` and `volume_change_1h_pct`, but current Polymarket market docs expose fields such as `volume24hr`, `oneHourPriceChange`, and `clobTokenIds`. The implementation should normalize current fields rather than assuming the document field names exist directly.

4. The WebSocket example is outdated.
The reviewed docs use a market subscription message with `"type": "market"` and require periodic `PING` heartbeats. The document example uses a `channel`-style payload and omits heartbeat handling.

5. Raw order-payload construction should not be the default path.
The document specifies low-level signed order bodies, but the official Polymarket Python client supports credential derivation, order-book access, and order placement. The build should prefer `py-clob-client` for correctness and reduce signing mistakes.

6. Risk-management requirements contradict the config section.
The document says kill switches are hardcoded and cannot be disabled via config, but later exposes those same thresholds as editable YAML values. The build should enforce kill-switch constants in code and keep YAML for tunable strategy parameters only.

7. The position-sizing explanation and code do not agree.
The document says score 7 should be 70% of max size, but the provided formula `(score - 6) / 4` yields `0.25` at score 7. That logic needs to be rewritten before live execution.

8. The trading-cost assumptions are not reliable enough to encode as-is.
The document treats daily gas costs as a direct scaling input, but Polymarket order placement is primarily a CLOB flow with distinct approval and settlement behavior. Cost controls should be measured from actual fills and allowance/setup events instead of hardcoded daily gas assumptions.

9. Several throughput targets are internally inconsistent.
The daily trade table, phase table, and compounding schedule disagree on the number of trades per day at comparable bankroll levels. The implementation should keep one source of truth for phase thresholds and trade targets.

## Current build interpretation

- Build order will follow the document sequence, especially the requirement to implement and test the risk manager before live order execution.
- The repository will start in paper-trading mode by default.
- Current external references should be validated against official docs during each integration step instead of assuming the document remains current.
