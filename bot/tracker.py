from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlparse

from api.catalyst import CatalystEvent
from db import queries
from api.spot import SpotSnapshot
from models.control import ProfileControlState, ResolvedControlState
from models import BotState, OutcomeSide, Position, PositionStatus, Trade, TradeOutcome


class TradeTracker:
    def __init__(self, database_url: str = "sqlite:///bot.db") -> None:
        self.database_url = database_url
        self.db_path = _database_path_from_url(database_url)

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        schema_path = Path(__file__).resolve().parents[1] / queries.CREATE_SCHEMA
        with self.connection() as conn:
            conn.executescript(schema_path.read_text())
            _ensure_schema_columns(conn)
            self._ensure_default_controls(conn)

    @contextmanager
    def connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record_trade(self, trade: Trade, *, notes: str | None = None) -> int:
        payload = {
            "timestamp": _serialize_datetime(trade.timestamp),
            "strategy_name": trade.strategy_name,
            "market_id": trade.market_id,
            "market_question": trade.market_question,
            "category": trade.category,
            "side": trade.side.value,
            "entry_price": trade.entry_price,
            "screened_price": trade.screened_price,
            "position_size": trade.position_size,
            "score": trade.score,
            "hours_to_close": trade.hours_to_close,
            "order_id": trade.order_id,
            "fill_price": trade.fill_price,
            "fill_slippage": trade.fill_slippage,
            "fill_slippage_pct": trade.fill_slippage_pct,
            "fill_time": _serialize_datetime(trade.fill_time),
            "outcome": trade.outcome.value,
            "resolution_price": trade.resolution_price,
            "pnl": trade.pnl,
            "execution_fee": trade.execution_fee,
            "notes": notes,
            "paper_trade": int(trade.paper_trade),
        }
        with self.connection() as conn:
            cursor = conn.execute(queries.INSERT_TRADE, payload)
            return int(cursor.lastrowid)

    def record_candidate_snapshots(self, snapshots: list[dict[str, object]]) -> None:
        if not snapshots:
            return
        with self.connection() as conn:
            conn.executemany(queries.INSERT_CANDIDATE_SNAPSHOT, snapshots)

    def record_order(
        self,
        *,
        order_id: str,
        market_id: str,
        status: str,
        requested_size: float,
        limit_price: float | None = None,
        filled_size: float = 0.0,
        last_seen_status: str | None = None,
        last_seen_at: datetime | None = None,
        exchange_payload: object | None = None,
        strategy_name: str | None = None,
    ) -> int:
        existing = self.get_order_by_order_id(order_id)
        merged = _merge_order_record(
            existing,
            {
                "order_id": order_id,
                "market_id": market_id,
                "strategy_name": strategy_name or (existing["strategy_name"] if existing is not None else None),
                "status": status,
                "requested_size": requested_size,
                "limit_price": limit_price,
                "filled_size": filled_size,
                "last_seen_status": last_seen_status or status,
                "last_seen_at": last_seen_at or datetime.now(UTC),
                "exchange_payload": exchange_payload,
            },
        )
        with self.connection() as conn:
            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO orders (
                      timestamp,
                      order_id,
                      strategy_name,
                      market_id,
                      status,
                      requested_size,
                      limit_price,
                      filled_size,
                      last_seen_status,
                      last_seen_at,
                      exchange_payload
                    )
                    VALUES (
                      :timestamp,
                      :order_id,
                      :strategy_name,
                      :market_id,
                      :status,
                      :requested_size,
                      :limit_price,
                      :filled_size,
                      :last_seen_status,
                      :last_seen_at,
                      :exchange_payload
                    )
                    """,
                    merged,
                )
                return int(cursor.lastrowid)
            conn.execute(
                """
                UPDATE orders
                SET timestamp = :timestamp,
                    strategy_name = :strategy_name,
                    market_id = :market_id,
                    status = :status,
                    requested_size = :requested_size,
                    limit_price = :limit_price,
                    filled_size = :filled_size,
                    last_seen_status = :last_seen_status,
                    last_seen_at = :last_seen_at,
                    exchange_payload = :exchange_payload
                WHERE order_id = :order_id
                """,
                merged,
            )
            row = conn.execute(
                "SELECT id FROM orders WHERE order_id = :order_id",
                {"order_id": order_id},
            ).fetchone()
            return int(row["id"]) if row is not None else 0

    def update_order_fill(
        self,
        *,
        order_id: str,
        market_id: str | None = None,
        requested_size: float | None = None,
        limit_price: float | None = None,
        filled_size: float,
        last_seen_status: str | None = None,
        last_seen_at: datetime | None = None,
        exchange_payload: object | None = None,
        strategy_name: str | None = None,
    ) -> int:
        existing = self.get_order_by_order_id(order_id)
        if existing is None and market_id is None and requested_size is None:
            raise ValueError("market_id or requested_size is required when creating a new order fill record")
        base_market_id = market_id or (existing["market_id"] if existing is not None else None)
        if requested_size is not None:
            base_requested_size = requested_size
        elif existing is not None:
            base_requested_size = float(existing["requested_size"])
        else:
            base_requested_size = filled_size
        if limit_price is not None:
            base_limit_price = limit_price
        elif existing is not None:
            base_limit_price = existing.get("limit_price")
        else:
            base_limit_price = None
        strategy_name = strategy_name or (existing["strategy_name"] if existing is not None else None)
        if base_market_id is None:
            raise ValueError("market_id is required to record an order fill")
        status = "FILLED" if filled_size >= base_requested_size and base_requested_size > 0 else "PARTIALLY_FILLED"
        return self.record_order(
            order_id=order_id,
            market_id=base_market_id,
            strategy_name=strategy_name,
            status=status,
            requested_size=base_requested_size,
            limit_price=float(base_limit_price) if base_limit_price is not None else None,
            filled_size=filled_size,
            last_seen_status=last_seen_status or status,
            last_seen_at=last_seen_at,
            exchange_payload=exchange_payload,
        )

    def list_open_orders(
        self,
        limit: int = 100,
        *,
        strategy_name: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        return self.list_orders(
            limit=limit,
            statuses=("PENDING", "OPEN", "LIVE_RESTING", "PARTIALLY_FILLED"),
            strategy_name=strategy_name,
        )

    def list_orders(
        self,
        *,
        limit: int = 100,
        statuses: tuple[str, ...] | list[str] | None = None,
        strategy_name: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        params: dict[str, object] = {"limit": limit}
        where_parts: list[str] = []
        if statuses:
            normalized = tuple(_normalize_order_status(str(status)) for status in statuses)
            placeholders = ", ".join(f":status_{index}" for index in range(len(normalized)))
            for index, status in enumerate(normalized):
                params[f"status_{index}"] = status
            where_parts.append(f"status IN ({placeholders})")
        if strategy_name is not None:
            params["strategy_name"] = strategy_name
            where_parts.append("strategy_name = :strategy_name")
        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                  id,
                  timestamp,
                  order_id,
                  strategy_name,
                  market_id,
                  status,
                  requested_size,
                  limit_price,
                  filled_size,
                  last_seen_status,
                  last_seen_at,
                  exchange_payload
                FROM orders
                {where_clause}
                ORDER BY COALESCE(last_seen_at, timestamp) ASC, id ASC
                LIMIT :limit
                """,
                params,
            ).fetchall()
        return tuple(_order_from_row(row) for row in rows)

    def list_recent_orders(
        self,
        *,
        limit: int = 100,
        statuses: tuple[str, ...] | list[str] | None = None,
        strategy_name: str | None = None,
    ) -> tuple[dict[str, object], ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        params: dict[str, object] = {"limit": limit}
        where_parts: list[str] = []
        if statuses:
            normalized = tuple(_normalize_order_status(str(status)) for status in statuses)
            placeholders = ", ".join(f":status_{index}" for index in range(len(normalized)))
            for index, status in enumerate(normalized):
                params[f"status_{index}"] = status
            where_parts.append(f"status IN ({placeholders})")
        if strategy_name is not None:
            params["strategy_name"] = strategy_name
            where_parts.append("strategy_name = :strategy_name")
        where_clause = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT
                  id,
                  timestamp,
                  order_id,
                  strategy_name,
                  market_id,
                  status,
                  requested_size,
                  limit_price,
                  filled_size,
                  last_seen_status,
                  last_seen_at,
                  exchange_payload
                FROM orders
                {where_clause}
                ORDER BY COALESCE(last_seen_at, timestamp) DESC, id DESC
                LIMIT :limit
                """,
                params,
            ).fetchall()
        return tuple(_order_from_row(row) for row in rows)

    def record_spot_tick(self, snapshot: SpotSnapshot) -> int:
        payload = {
            "timestamp": _serialize_datetime(datetime.now(UTC)),
            "symbol": snapshot.symbol,
            "pair": snapshot.pair,
            "source": snapshot.source,
            "fetch_latency_seconds": snapshot.fetch_latency_seconds,
            "observed_at": _serialize_datetime(snapshot.observed_at),
            "spot_price": snapshot.spot_price,
            "return_15m_pct": snapshot.return_15m_pct,
            "return_1h_pct": snapshot.return_1h_pct,
            "age_seconds": snapshot.age_seconds,
            "raw_payload": json.dumps(snapshot.payload) if snapshot.payload is not None else None,
        }
        with self.connection() as conn:
            cursor = conn.execute(queries.INSERT_SPOT_TICK, payload)
            return int(cursor.lastrowid)

    def record_catalyst_events(
        self,
        events: tuple[CatalystEvent, ...] | list[CatalystEvent],
        *,
        provider: str,
    ) -> None:
        if not events:
            return
        payloads = []
        for event in events:
            payloads.append(
                {
                    "timestamp": _serialize_datetime(datetime.now(UTC)),
                    "provider": provider,
                    "calendar_id": event.calendar_id,
                    "country": event.country,
                    "category": event.category,
                    "event": event.event,
                    "event_time": _serialize_datetime(event.date),
                    "importance": event.importance,
                    "source": event.source,
                    "source_url": event.source_url,
                    "url": event.url,
                    "reference": event.reference,
                    "reference_date": _serialize_datetime(event.reference_date),
                    "actual": event.actual,
                    "previous": event.previous,
                    "forecast": event.forecast,
                    "te_forecast": event.te_forecast,
                    "ticker": event.ticker,
                    "symbol": event.symbol,
                    "currency": event.currency,
                    "unit": event.unit,
                    "last_update": _serialize_datetime(event.last_update),
                    "raw_payload": json.dumps(event.payload) if event.payload is not None else None,
                }
            )
        with self.connection() as conn:
            conn.executemany(queries.INSERT_CATALYST_EVENT, payloads)

    def list_recent_catalyst_events(self, provider: str, limit: int = 100) -> tuple[CatalystEvent, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.connection() as conn:
            rows = conn.execute(
                queries.SELECT_RECENT_CATALYST_EVENTS,
                {"provider": provider, "limit": limit},
            ).fetchall()
        return tuple(_catalyst_event_from_row(row) for row in rows)

    def get_latest_spot_snapshot(self, symbol: str) -> SpotSnapshot | None:
        with self.connection() as conn:
            row = conn.execute(
                queries.SELECT_LATEST_SPOT_TICK,
                {"symbol": symbol.strip().upper()},
            ).fetchone()
        if row is None:
            return None
        return _spot_snapshot_from_row(row)

    def list_recent_spot_snapshots(self, symbol: str, limit: int = 100) -> tuple[SpotSnapshot, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.connection() as conn:
            rows = conn.execute(
                queries.SELECT_RECENT_SPOT_TICKS,
                {"symbol": symbol.strip().upper(), "limit": limit},
            ).fetchall()
        return tuple(_spot_snapshot_from_row(row) for row in rows)

    def open_order_count(self, strategy_name: str | None = None) -> int:
        query = "SELECT COUNT(*) AS count FROM orders WHERE status IN ('PENDING', 'OPEN', 'LIVE_RESTING', 'PARTIALLY_FILLED')"
        params: dict[str, object] = {}
        if strategy_name is not None:
            query += " AND strategy_name = :strategy_name"
            params["strategy_name"] = strategy_name
        with self.connection() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["count"])

    def close_order(
        self,
        *,
        order_id: str,
        status: str = "CANCELLED",
        last_seen_status: str | None = None,
        last_seen_at: datetime | None = None,
        filled_size: float | None = None,
        exchange_payload: object | None = None,
        strategy_name: str | None = None,
    ) -> int:
        existing = self.get_order_by_order_id(order_id)
        if existing is None:
            raise ValueError(f"Unknown order_id: {order_id}")
        merged_filled = float(existing["filled_size"]) if filled_size is None else max(float(existing["filled_size"]), filled_size)
        return self.record_order(
            order_id=order_id,
            market_id=str(existing["market_id"]),
            strategy_name=strategy_name or existing.get("strategy_name"),
            status=status,
            requested_size=float(existing["requested_size"]),
            limit_price=float(existing["limit_price"]) if existing.get("limit_price") is not None else None,
            filled_size=merged_filled,
            last_seen_status=last_seen_status or status,
            last_seen_at=last_seen_at,
            exchange_payload=exchange_payload if exchange_payload is not None else existing.get("exchange_payload"),
        )

    def update_trade_resolution(
        self,
        *,
        order_id: str,
        outcome: TradeOutcome,
        resolution_price: float,
        pnl: float,
        resolved_at: datetime,
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE trades
                SET outcome = :outcome,
                    resolution_price = :resolution_price,
                    pnl = :pnl,
                    fill_time = COALESCE(fill_time, :resolved_at)
                WHERE order_id = :order_id
                """,
                {
                    "order_id": order_id,
                    "outcome": outcome.value,
                    "resolution_price": resolution_price,
                    "pnl": pnl,
                    "resolved_at": _serialize_datetime(resolved_at),
                },
            )

    def upsert_position(self, position: Position, *, notes: str | None = None) -> int:
        payload = {
            "id": position.position_id,
            "timestamp": _serialize_datetime(position.timestamp),
            "strategy_name": position.strategy_name,
            "market_id": position.market_id,
            "market_question": position.market_question,
            "category": position.category,
            "side": position.side.value,
            "fill_price": position.fill_price,
            "shares": position.shares,
            "cost_basis": position.cost_basis,
            "order_id": position.order_id,
            "status": position.status.value,
            "resolved_at": _serialize_datetime(position.resolved_at),
            "resolution_price": position.resolution_price,
            "pnl": position.pnl,
            "paper_trade": int(position.paper_trade),
            "notes": notes,
        }
        with self.connection() as conn:
            if position.position_id is not None and position.order_id is None:
                conn.execute(
                    """
                    UPDATE positions
                    SET timestamp = :timestamp,
                        strategy_name = :strategy_name,
                        market_id = :market_id,
                        market_question = :market_question,
                        category = :category,
                        side = :side,
                        fill_price = :fill_price,
                        shares = :shares,
                        cost_basis = :cost_basis,
                        status = :status,
                        resolved_at = :resolved_at,
                        resolution_price = :resolution_price,
                        pnl = :pnl,
                        paper_trade = :paper_trade,
                        notes = :notes
                    WHERE id = :id
                    """,
                    payload,
                )
                return position.position_id
            cursor = conn.execute(queries.UPSERT_POSITION, payload)
            if position.order_id:
                row = conn.execute(
                    "SELECT id FROM positions WHERE order_id = :order_id",
                    {"order_id": position.order_id},
                ).fetchone()
                if row is not None:
                    return int(row["id"])
            return int(cursor.lastrowid or 0)

    def get_position_by_order_id(self, order_id: str) -> Position | None:
        with self.connection() as conn:
            row = conn.execute(
                queries.SELECT_POSITION_BY_ORDER_ID,
                {"order_id": order_id},
            ).fetchone()
        if row is None:
            return None
        return _position_from_row(row)

    def list_open_positions(self, limit: int = 100) -> tuple[Position, ...]:
        return self.list_open_positions_for_strategy(None, limit=limit)

    def list_open_positions_for_strategy(
        self,
        strategy_name: str | None,
        *,
        limit: int = 100,
    ) -> tuple[Position, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        query = queries.SELECT_OPEN_POSITIONS
        params: dict[str, object] = {"limit": limit}
        if strategy_name is not None:
            query = query.replace("WHERE status = 'OPEN'", "WHERE status = 'OPEN' AND strategy_name = :strategy_name")
            params["strategy_name"] = strategy_name
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(_position_from_row(row) for row in rows)

    def list_recent_positions(self, limit: int = 100) -> tuple[Position, ...]:
        return self.list_recent_positions_for_strategy(None, limit=limit)

    def list_recent_positions_for_strategy(
        self,
        strategy_name: str | None,
        *,
        limit: int = 100,
    ) -> tuple[Position, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        query = queries.SELECT_RECENT_POSITIONS
        params: dict[str, object] = {"limit": limit}
        if strategy_name is not None:
            query = query.replace("FROM positions", "FROM positions WHERE strategy_name = :strategy_name")
            params["strategy_name"] = strategy_name
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        positions = tuple(_position_from_row(row) for row in rows)
        return tuple(reversed(positions))

    def open_position_count(self, strategy_name: str | None = None) -> int:
        query = "SELECT COUNT(*) AS count FROM positions WHERE status = 'OPEN'"
        params: dict[str, object] = {}
        if strategy_name is not None:
            query += " AND strategy_name = :strategy_name"
            params["strategy_name"] = strategy_name
        with self.connection() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["count"])

    def list_recent_trades(self, limit: int = 100, *, strategy_name: str | None = None) -> tuple[Trade, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        query = queries.SELECT_RECENT_TRADES
        params: dict[str, object] = {"limit": limit}
        if strategy_name is not None:
            query = query.replace("FROM trades", "FROM trades WHERE strategy_name = :strategy_name")
            params["strategy_name"] = strategy_name
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        trades = tuple(_trade_from_row(row) for row in rows)
        return tuple(reversed(trades))

    def record_state(self, state: BotState) -> int:
        payload = {
            "timestamp": _serialize_datetime(state.timestamp),
            "strategy_name": state.strategy_name,
            "bankroll": state.bankroll,
            "phase": state.phase,
            "bankroll_start_of_day": state.bankroll_start_of_day,
            "bankroll_start_of_week": state.bankroll_start_of_week,
            "daily_pnl": state.daily_pnl,
            "weekly_pnl": state.weekly_pnl,
            "total_trades": state.total_trades,
            "consecutive_failures": state.consecutive_failures,
            "open_orders": state.open_orders,
            "open_positions": state.open_positions,
            "win_rate_50": self.win_rate(50, strategy_name=state.strategy_name),
            "win_rate_100": self.win_rate(100, strategy_name=state.strategy_name),
            "win_rate_all": self.win_rate_all(strategy_name=state.strategy_name),
            "strategy_min_price": state.strategy_min_price,
            "strategy_min_score": state.strategy_min_score,
            "is_paused": int(state.is_paused),
            "pause_level": state.pause_level,
            "pause_reason": state.pause_reason,
            "pause_until": _serialize_datetime(state.pause_until),
        }
        with self.connection() as conn:
            cursor = conn.execute(queries.INSERT_STATE, payload)
            return int(cursor.lastrowid)

    def get_control_state(self, profile_name: str | None = None) -> ProfileControlState | None:
        control_key = _control_key(profile_name)
        with self.connection() as conn:
            row = conn.execute(
                queries.SELECT_PROFILE_CONTROL,
                {"control_key": control_key},
            ).fetchone()
        if row is None:
            return None
        return _control_state_from_row(row)

    def list_control_states(self) -> tuple[ProfileControlState, ...]:
        with self.connection() as conn:
            rows = conn.execute(queries.SELECT_ALL_PROFILE_CONTROLS).fetchall()
        return tuple(_control_state_from_row(row) for row in rows)

    def get_telegram_router_state(self) -> dict[str, object] | None:
        with self.connection() as conn:
            row = conn.execute(queries.SELECT_TELEGRAM_ROUTER_STATE).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "last_update_id": int(row["last_update_id"]),
            "updated_at": _parse_datetime(row["updated_at"]),
            "last_error": row["last_error"],
        }

    def set_telegram_router_state(
        self,
        *,
        last_update_id: int,
        last_error: str | None = None,
    ) -> None:
        payload = {
            "last_update_id": last_update_id,
            "updated_at": _serialize_datetime(datetime.now(UTC)),
            "last_error": last_error,
        }
        with self.connection() as conn:
            conn.execute(queries.UPSERT_TELEGRAM_ROUTER_STATE, payload)

    def upsert_control_state(
        self,
        *,
        profile_name: str | None,
        desired_state: str,
        run_once_pending: int | None = None,
        run_once_delta: int = 0,
        updated_by: str | None = None,
        source_chat_id: str | None = None,
        source_message_id: int | None = None,
        last_command: str | None = None,
        notes: str | None = None,
    ) -> ProfileControlState:
        control_key = _control_key(profile_name)
        scope = "GLOBAL" if profile_name is None else "PROFILE"
        existing = self.get_control_state(profile_name)
        pending = run_once_pending
        if pending is None:
            base_pending = existing.run_once_pending if existing is not None else 0
            pending = max(0, base_pending + run_once_delta)
        payload = {
            "control_key": control_key,
            "scope": scope,
            "profile_name": profile_name,
            "desired_state": _normalize_control_state(desired_state),
            "run_once_pending": int(pending),
            "updated_at": _serialize_datetime(datetime.now(UTC)),
            "updated_by": updated_by,
            "source_chat_id": source_chat_id,
            "source_message_id": source_message_id,
            "last_command": last_command,
            "notes": notes,
        }
        with self.connection() as conn:
            conn.execute(queries.UPSERT_PROFILE_CONTROL, payload)
        return self.get_control_state(profile_name) or ProfileControlState(
            control_key=control_key,
            scope=scope,
            profile_name=profile_name,
            desired_state=payload["desired_state"],
            run_once_pending=payload["run_once_pending"],
            updated_at=datetime.now(UTC),
            updated_by=updated_by,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            last_command=last_command,
            notes=notes,
        )

    def consume_run_once(self, profile_name: str, count: int = 1) -> ProfileControlState | None:
        if count <= 0:
            raise ValueError("count must be positive")
        current = self.get_control_state(profile_name)
        if current is None:
            return None
        pending = max(0, current.run_once_pending - count)
        return self.upsert_control_state(
            profile_name=profile_name,
            desired_state=current.desired_state,
            run_once_pending=pending,
            updated_by=current.updated_by,
            source_chat_id=current.source_chat_id,
            source_message_id=current.source_message_id,
            last_command=current.last_command,
            notes=current.notes,
        )

    def resolve_control_state(self, profile_name: str) -> ResolvedControlState:
        global_state = self.get_control_state(None) or ProfileControlState(
            control_key="GLOBAL",
            scope="GLOBAL",
            profile_name=None,
            desired_state="RUNNING",
            run_once_pending=0,
        )
        profile_state = self.get_control_state(profile_name) or ProfileControlState(
            control_key=profile_name,
            scope="PROFILE",
            profile_name=profile_name,
            desired_state="RUNNING",
            run_once_pending=0,
        )
        if global_state.desired_state == "STOPPED" or profile_state.desired_state == "STOPPED":
            effective_state = "STOPPED"
        elif global_state.desired_state == "PAUSED" or profile_state.desired_state == "PAUSED":
            effective_state = "PAUSED"
        else:
            effective_state = "RUNNING"
        return ResolvedControlState(
            profile_name=profile_name,
            global_state=global_state,
            profile_state=profile_state,
            effective_state=effective_state,
            run_once_pending=profile_state.run_once_pending,
        )

    def build_state_snapshot(
        self,
        *,
        bankroll: float,
        phase: int,
        strategy_min_price: float,
        strategy_min_score: float,
        strategy_name: str | None = None,
        timestamp: datetime | None = None,
        open_orders: int | None = None,
        open_positions: int | None = None,
        is_paused: bool = False,
        pause_level: str | None = None,
        pause_reason: str | None = None,
        pause_until: datetime | None = None,
        recent_trade_limit: int = 100,
    ) -> BotState:
        snapshot_time = timestamp or datetime.now(UTC)
        all_trades = self.list_recent_trades(limit=max(1, self.trade_count(strategy_name=strategy_name)), strategy_name=strategy_name)
        recent_trades = all_trades[-recent_trade_limit:]
        if open_orders is None:
            open_orders = self.open_order_count(strategy_name)
        if open_positions is None:
            open_positions = self.open_position_count(strategy_name)
        daily_pnl = round(
            sum((trade.pnl or 0.0) for trade in all_trades if _utc_day(trade.timestamp) == _utc_day(snapshot_time)),
            6,
        )
        weekly_pnl = round(
            sum((trade.pnl or 0.0) for trade in all_trades if _iso_week(trade.timestamp) == _iso_week(snapshot_time)),
            6,
        )

        return BotState(
            timestamp=snapshot_time,
            bankroll=round(bankroll, 6),
            phase=phase,
            bankroll_start_of_day=round(bankroll - daily_pnl, 6),
            bankroll_start_of_week=round(bankroll - weekly_pnl, 6),
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            total_trades=len(all_trades),
            consecutive_failures=_consecutive_failures(all_trades),
            open_orders=open_orders,
            open_positions=open_positions,
            strategy_min_price=strategy_min_price,
            strategy_min_score=strategy_min_score,
            strategy_name=strategy_name,
            is_paused=is_paused,
            pause_level=pause_level,
            pause_reason=pause_reason,
            pause_until=pause_until,
            recent_trades=tuple(recent_trades),
        )

    def get_latest_state(
        self,
        *,
        recent_trade_limit: int = 100,
        strategy_name: str | None = None,
    ) -> BotState | None:
        query = queries.SELECT_LATEST_STATE
        params: dict[str, object] = {}
        if strategy_name is not None:
            query = query.replace("FROM state", "FROM state WHERE strategy_name = :strategy_name")
            params["strategy_name"] = strategy_name
        with self.connection() as conn:
            row = conn.execute(query, params).fetchone()
        if row is None:
            return None

        recent_trades = self.list_recent_trades(limit=recent_trade_limit, strategy_name=strategy_name)
        return BotState(
            timestamp=_parse_datetime(row["timestamp"]),
            strategy_name=row["strategy_name"],
            bankroll=float(row["bankroll"]),
            phase=int(row["phase"]),
            bankroll_start_of_day=float(row["bankroll_start_of_day"]),
            bankroll_start_of_week=float(row["bankroll_start_of_week"]),
            daily_pnl=float(row["daily_pnl"]),
            weekly_pnl=float(row["weekly_pnl"]),
            total_trades=int(row["total_trades"]),
            consecutive_failures=int(row["consecutive_failures"]),
            open_orders=int(row["open_orders"]),
            open_positions=int(row["open_positions"]),
            strategy_min_price=float(row["strategy_min_price"]),
            strategy_min_score=float(row["strategy_min_score"]),
            is_paused=bool(row["is_paused"]),
            pause_level=row["pause_level"],
            pause_reason=row["pause_reason"],
            pause_until=_parse_datetime(row["pause_until"]),
            recent_trades=recent_trades,
        )

    def trade_count(self, strategy_name: str | None = None) -> int:
        query = "SELECT COUNT(*) AS count FROM trades"
        params: dict[str, object] = {}
        if strategy_name is not None:
            query += " WHERE strategy_name = :strategy_name"
            params["strategy_name"] = strategy_name
        with self.connection() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["count"])

    def get_order_by_order_id(self, order_id: str) -> dict[str, object] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT
                  id,
                  timestamp,
                  order_id,
                  strategy_name,
                  market_id,
                  status,
                  requested_size,
                  limit_price,
                  filled_size,
                  last_seen_status,
                  last_seen_at,
                  exchange_payload
                FROM orders
                WHERE order_id = :order_id
                LIMIT 1
                """,
                {"order_id": order_id},
            ).fetchone()
        if row is None:
            return None
        return _order_from_row(row)

    def register_open_position_from_trade(self, trade: Trade) -> int | None:
        if trade.outcome is not TradeOutcome.PENDING or trade.fill_price is None:
            return None
        shares = round(trade.position_size / trade.fill_price, 6)
        position = Position(
            timestamp=trade.fill_time or trade.timestamp,
            market_id=trade.market_id,
            market_question=trade.market_question,
            category=trade.category,
            side=trade.side,
            fill_price=trade.fill_price,
            shares=shares,
            cost_basis=trade.position_size,
            order_id=trade.order_id,
            strategy_name=trade.strategy_name,
            paper_trade=trade.paper_trade,
        )
        return self.upsert_position(position)

    def win_rate(self, limit: int, strategy_name: str | None = None) -> float | None:
        resolved = self._resolved_trade_pnls(limit, strategy_name=strategy_name)
        if not resolved:
            return None
        wins = sum(1 for pnl in resolved if pnl > 0)
        return wins / len(resolved)

    def win_rate_all(self, strategy_name: str | None = None) -> float | None:
        query = "SELECT pnl FROM trades WHERE pnl IS NOT NULL"
        params: dict[str, object] = {}
        if strategy_name is not None:
            query += " AND strategy_name = :strategy_name"
            params["strategy_name"] = strategy_name
        with self.connection() as conn:
            rows = conn.execute(
                query + " ORDER BY timestamp DESC, id DESC",
                params,
            ).fetchall()
        if not rows:
            return None
        pnls = [float(row["pnl"]) for row in rows]
        wins = sum(1 for pnl in pnls if pnl > 0)
        return wins / len(pnls)

    def _resolved_trade_pnls(self, limit: int, *, strategy_name: str | None = None) -> list[float]:
        query = """
        SELECT pnl
        FROM trades
        WHERE pnl IS NOT NULL
        """
        params: dict[str, object] = {"limit": limit}
        if strategy_name is not None:
            query += " AND strategy_name = :strategy_name"
            params["strategy_name"] = strategy_name
        query += " ORDER BY timestamp DESC, id DESC LIMIT :limit"
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [float(row["pnl"]) for row in rows]

    def candidate_performance_report(self) -> dict[str, object]:
        with self.connection() as conn:
            snapshot_rows = conn.execute(
                """
                SELECT category, score, final_score, selected, cluster_key, template_key
                FROM candidate_snapshots
                """
            ).fetchall()
            trade_rows = conn.execute(
                """
                SELECT category, score, outcome, pnl
                FROM trades
                """
            ).fetchall()

        category_counts: dict[str, int] = {}
        score_bands = {"7.0-7.99": 0, "8.0-8.99": 0, "9.0-10.0": 0}
        selected_count = 0
        for row in snapshot_rows:
            category = row["category"] or "unknown"
            category_counts[category] = category_counts.get(category, 0) + 1
            band = _score_band(float(row["final_score"] if row["final_score"] is not None else row["score"] or 0.0))
            if band is not None:
                score_bands[band] += 1
            selected_count += int(row["selected"] or 0)

        trade_summary: dict[str, dict[str, float | int | None]] = {}
        for row in trade_rows:
            category = row["category"] or "unknown"
            bucket = trade_summary.setdefault(
                category,
                {"trades": 0, "wins": 0, "losses": 0, "cancelled": 0, "avg_pnl": None},
            )
            bucket["trades"] = int(bucket["trades"]) + 1
            outcome = row["outcome"]
            if outcome == "WIN":
                bucket["wins"] = int(bucket["wins"]) + 1
            elif outcome == "LOSS":
                bucket["losses"] = int(bucket["losses"]) + 1
            elif outcome == "CANCELLED":
                bucket["cancelled"] = int(bucket["cancelled"]) + 1

        pnl_by_category: dict[str, list[float]] = {}
        for row in trade_rows:
            if row["pnl"] is None:
                continue
            category = row["category"] or "unknown"
            pnl_by_category.setdefault(category, []).append(float(row["pnl"]))
        for category, values in pnl_by_category.items():
            trade_summary.setdefault(
                category,
                {"trades": 0, "wins": 0, "losses": 0, "cancelled": 0, "avg_pnl": None},
            )["avg_pnl"] = round(sum(values) / len(values), 6)

        return {
            "snapshot_count": len(snapshot_rows),
            "selected_snapshot_count": selected_count,
            "snapshot_categories": sorted(category_counts.items(), key=lambda item: item[1], reverse=True),
            "snapshot_score_bands": score_bands,
            "trade_summary_by_category": trade_summary,
        }

    def profile_performance_report(self) -> list[dict[str, object]]:
        with self.connection() as conn:
            state_rows = conn.execute(
                """
                SELECT strategy_name
                FROM state
                GROUP BY strategy_name
                """
            ).fetchall()
            trade_rows = conn.execute(
                """
                SELECT
                  strategy_name,
                  COUNT(*) AS trade_count,
                  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                  SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses,
                  SUM(CASE WHEN pnl IS NULL THEN 1 ELSE 0 END) AS unresolved,
                  SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END) AS total_pnl
                FROM trades
                GROUP BY strategy_name
                ORDER BY COALESCE(strategy_name, 'unassigned') ASC
                """
            ).fetchall()
            order_rows = conn.execute(
                """
                SELECT strategy_name, COUNT(*) AS open_orders
                FROM orders
                WHERE status IN ('PENDING', 'OPEN', 'LIVE_RESTING', 'PARTIALLY_FILLED')
                GROUP BY strategy_name
                """
            ).fetchall()
            position_rows = conn.execute(
                """
                SELECT strategy_name, COUNT(*) AS open_positions
                FROM positions
                WHERE status = 'OPEN'
                GROUP BY strategy_name
                """
            ).fetchall()

        strategies = {
            row["strategy_name"] or "unassigned"
            for row in state_rows
        }
        strategies.update(row["strategy_name"] or "unassigned" for row in trade_rows)
        strategies.update(row["strategy_name"] or "unassigned" for row in order_rows)
        strategies.update(row["strategy_name"] or "unassigned" for row in position_rows)

        open_orders_by_strategy = {
            (row["strategy_name"] or "unassigned"): int(row["open_orders"])
            for row in order_rows
        }
        open_positions_by_strategy = {
            (row["strategy_name"] or "unassigned"): int(row["open_positions"])
            for row in position_rows
        }
        report: list[dict[str, object]] = []
        for row in trade_rows:
            strategy = row["strategy_name"] or "unassigned"
            trade_count = int(row["trade_count"])
            wins = int(row["wins"] or 0)
            losses = int(row["losses"] or 0)
            unresolved = int(row["unresolved"] or 0)
            total_pnl = float(row["total_pnl"] or 0.0)
            resolved = wins + losses
            report.append(
                {
                    "strategy_name": strategy,
                    "trade_count": trade_count,
                    "wins": wins,
                    "losses": losses,
                    "unresolved": unresolved,
                    "open_orders": open_orders_by_strategy.get(strategy, 0),
                    "open_positions": open_positions_by_strategy.get(strategy, 0),
                    "win_rate": None if resolved == 0 else wins / resolved,
                    "total_pnl": round(total_pnl, 6),
                }
            )
        if not report:
            report.append(
                {
                    "strategy_name": "unassigned",
                    "trade_count": 0,
                    "wins": 0,
                    "losses": 0,
                    "unresolved": 0,
                    "open_orders": 0,
                    "open_positions": 0,
                    "win_rate": None,
                    "total_pnl": 0.0,
                }
            )
        known = {row["strategy_name"] for row in report}
        for strategy in sorted(strategies - known):
            report.append(
                {
                    "strategy_name": strategy,
                    "trade_count": 0,
                    "wins": 0,
                    "losses": 0,
                    "unresolved": 0,
                    "open_orders": open_orders_by_strategy.get(strategy, 0),
                    "open_positions": open_positions_by_strategy.get(strategy, 0),
                    "win_rate": None,
                    "total_pnl": 0.0,
                }
            )
        return report

    def _ensure_default_controls(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            queries.UPSERT_PROFILE_CONTROL,
            {
                "control_key": "GLOBAL",
                "scope": "GLOBAL",
                "profile_name": None,
                "desired_state": "RUNNING",
                "run_once_pending": 0,
                "updated_at": _serialize_datetime(datetime.now(UTC)),
                "updated_by": "bootstrap",
                "source_chat_id": None,
                "source_message_id": None,
                "last_command": "bootstrap",
                "notes": "Default global control state",
            },
        )


def _ensure_schema_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(
        conn,
        "trades",
        (
            ("strategy_name", "TEXT"),
            ("screened_price", "REAL"),
            ("fill_slippage", "REAL"),
            ("fill_slippage_pct", "REAL"),
        ),
    )
    _ensure_columns(
        conn,
        "orders",
        (
            ("strategy_name", "TEXT"),
            ("limit_price", "REAL"),
        ),
    )
    _ensure_columns(
        conn,
        "state",
        (
            ("strategy_name", "TEXT"),
        ),
    )
    _ensure_columns(
        conn,
        "positions",
        (
            ("strategy_name", "TEXT"),
        ),
    )


def _ensure_columns(
    conn: sqlite3.Connection,
    table_name: str,
    columns: tuple[tuple[str, str], ...],
) -> None:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing = {str(row["name"]) for row in rows}
    for column_name, column_type in columns:
        if column_name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def _control_key(profile_name: str | None) -> str:
    return "GLOBAL" if profile_name is None else profile_name


def _normalize_control_state(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in {"RUNNING", "PAUSED", "STOPPED"}:
        raise ValueError("desired_state must be RUNNING, PAUSED, or STOPPED")
    return normalized


def _database_path_from_url(database_url: str) -> Path:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        raise ValueError("Only sqlite URLs are supported at this stage")
    if not parsed.path:
        raise ValueError("sqlite database URL must include a path")
    raw_path = parsed.path
    if raw_path.startswith("/"):
        raw_path = raw_path[1:]
    return Path(raw_path)


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Expected datetime column to be stored as text")
    return datetime.fromisoformat(value)


def _trade_from_row(row: sqlite3.Row) -> Trade:
    return Trade(
        timestamp=_parse_datetime(row["timestamp"]) or datetime.now(UTC),
        strategy_name=row["strategy_name"],
        market_id=row["market_id"],
        market_question=row["market_question"],
        category=row["category"],
        side=OutcomeSide(row["side"]),
        entry_price=float(row["entry_price"]),
        screened_price=float(row["screened_price"]) if row["screened_price"] is not None else None,
        position_size=float(row["position_size"]),
        score=float(row["score"]) if row["score"] is not None else None,
        hours_to_close=float(row["hours_to_close"]) if row["hours_to_close"] is not None else None,
        order_id=row["order_id"],
        fill_price=float(row["fill_price"]) if row["fill_price"] is not None else None,
        fill_slippage=float(row["fill_slippage"]) if row["fill_slippage"] is not None else None,
        fill_slippage_pct=float(row["fill_slippage_pct"]) if row["fill_slippage_pct"] is not None else None,
        fill_time=_parse_datetime(row["fill_time"]),
        outcome=TradeOutcome(row["outcome"]) if row["outcome"] is not None else TradeOutcome.PENDING,
        resolution_price=float(row["resolution_price"]) if row["resolution_price"] is not None else None,
        pnl=float(row["pnl"]) if row["pnl"] is not None else None,
        execution_fee=float(row["execution_fee"] or 0.0),
        paper_trade=bool(row["paper_trade"]),
    )


def _position_from_row(row: sqlite3.Row) -> Position:
    return Position(
        position_id=int(row["id"]) if row["id"] is not None else None,
        timestamp=_parse_datetime(row["timestamp"]) or datetime.now(UTC),
        strategy_name=row["strategy_name"],
        market_id=row["market_id"],
        market_question=row["market_question"],
        category=row["category"],
        side=OutcomeSide(row["side"]),
        fill_price=float(row["fill_price"]),
        shares=float(row["shares"]),
        cost_basis=float(row["cost_basis"]),
        order_id=row["order_id"],
        status=PositionStatus(row["status"]),
        resolved_at=_parse_datetime(row["resolved_at"]),
        resolution_price=float(row["resolution_price"]) if row["resolution_price"] is not None else None,
        pnl=float(row["pnl"]) if row["pnl"] is not None else None,
        paper_trade=bool(row["paper_trade"]),
    )


def _order_from_row(row: sqlite3.Row) -> dict[str, object]:
    payload = row["exchange_payload"]
    parsed_payload: object
    if payload is None:
        parsed_payload = None
    elif isinstance(payload, str):
        try:
            parsed_payload = json.loads(payload)
        except json.JSONDecodeError:
            parsed_payload = payload
    else:
        parsed_payload = payload
    return {
        "id": int(row["id"]) if row["id"] is not None else None,
        "timestamp": _parse_datetime(row["timestamp"]),
        "order_id": row["order_id"],
        "strategy_name": row["strategy_name"],
        "market_id": row["market_id"],
        "status": row["status"],
        "requested_size": float(row["requested_size"]),
        "limit_price": float(row["limit_price"]) if row["limit_price"] is not None else None,
        "filled_size": float(row["filled_size"]),
        "last_seen_status": row["last_seen_status"],
        "last_seen_at": _parse_datetime(row["last_seen_at"]),
        "exchange_payload": parsed_payload,
    }


def _control_state_from_row(row: sqlite3.Row) -> ProfileControlState:
    return ProfileControlState(
        control_key=str(row["control_key"]),
        scope=str(row["scope"]),
        profile_name=row["profile_name"],
        desired_state=str(row["desired_state"]),
        run_once_pending=int(row["run_once_pending"] or 0),
        updated_at=_parse_datetime(row["updated_at"]),
        updated_by=row["updated_by"],
        source_chat_id=row["source_chat_id"],
        source_message_id=int(row["source_message_id"]) if row["source_message_id"] is not None else None,
        last_command=row["last_command"],
        notes=row["notes"],
    )


def _spot_snapshot_from_row(row: sqlite3.Row) -> SpotSnapshot:
    payload = row["raw_payload"]
    parsed_payload: object
    if payload is None:
        parsed_payload = None
    elif isinstance(payload, str):
        try:
            parsed_payload = json.loads(payload)
        except json.JSONDecodeError:
            parsed_payload = payload
    else:
        parsed_payload = payload
    observed_at = _parse_datetime(row["observed_at"]) or datetime.now(UTC)
    return SpotSnapshot(
        symbol=str(row["symbol"]),
        pair=str(row["pair"]),
        spot_price=float(row["spot_price"]),
        return_15m_pct=float(row["return_15m_pct"]) if row["return_15m_pct"] is not None else None,
        return_1h_pct=float(row["return_1h_pct"]) if row["return_1h_pct"] is not None else None,
        fetch_latency_seconds=float(row["fetch_latency_seconds"]) if row["fetch_latency_seconds"] is not None else None,
        observed_at=observed_at,
        age_seconds=max(0.0, (datetime.now(UTC) - observed_at).total_seconds()),
        source=str(row["source"]),
        payload=parsed_payload if isinstance(parsed_payload, dict) else None,
    )


def _catalyst_event_from_row(row: sqlite3.Row) -> CatalystEvent:
    payload = row["raw_payload"]
    parsed_payload: object
    if payload is None:
        parsed_payload = None
    elif isinstance(payload, str):
        try:
            parsed_payload = json.loads(payload)
        except json.JSONDecodeError:
            parsed_payload = payload
    else:
        parsed_payload = payload
    return CatalystEvent(
        calendar_id=str(row["calendar_id"]),
        country=str(row["country"]),
        category=str(row["category"]),
        event=str(row["event"]),
        date=_parse_datetime(row["event_time"]) or datetime.now(UTC),
        importance=int(row["importance"]),
        source=row["source"],
        source_url=row["source_url"],
        url=row["url"],
        reference=row["reference"],
        reference_date=_parse_datetime(row["reference_date"]),
        actual=row["actual"],
        previous=row["previous"],
        forecast=row["forecast"],
        te_forecast=row["te_forecast"],
        ticker=row["ticker"],
        symbol=row["symbol"],
        currency=row["currency"],
        unit=row["unit"],
        last_update=_parse_datetime(row["last_update"]),
        payload=parsed_payload if isinstance(parsed_payload, dict) else None,
    )


def _merge_order_record(
    existing: dict[str, object] | None,
    incoming: dict[str, object],
) -> dict[str, object]:
    requested_size = max(
        float(existing["requested_size"]) if existing is not None else 0.0,
        float(incoming["requested_size"]),
    )
    incoming_limit_price = incoming.get("limit_price")
    if incoming_limit_price is not None:
        limit_price = float(incoming_limit_price)
    elif existing is not None:
        existing_limit_price = existing.get("limit_price")
        limit_price = float(existing_limit_price) if existing_limit_price is not None else None
    else:
        limit_price = None
    filled_size = max(
        float(existing["filled_size"]) if existing is not None else 0.0,
        float(incoming["filled_size"]),
    )
    incoming_status = _normalize_order_status(str(incoming["status"]))
    existing_status = _normalize_order_status(str(existing["status"])) if existing is not None else None
    merged_status = incoming_status
    if filled_size > 0 and requested_size > 0 and filled_size + 1e-9 >= requested_size:
        merged_status = "FILLED"
    elif existing_status is not None and _order_status_rank(incoming_status) < _order_status_rank(existing_status):
        existing_seen = _coerce_datetime(existing["last_seen_at"]) if existing is not None else None
        incoming_seen = _coerce_datetime(incoming["last_seen_at"]) or datetime.now(UTC)
        if filled_size <= float(existing["filled_size"]) and (
            existing_seen is None or incoming_seen <= existing_seen
        ):
            merged_status = existing_status
    elif filled_size > 0 and incoming_status in {"OPEN", "PENDING"}:
        merged_status = "PARTIALLY_FILLED"

    timestamp = _coerce_datetime(incoming["last_seen_at"]) or datetime.now(UTC)
    existing_seen = _coerce_datetime(existing["last_seen_at"]) if existing is not None else None
    if existing_seen is not None and existing_seen > timestamp:
        timestamp = existing_seen

    exchange_payload = incoming.get("exchange_payload")
    if exchange_payload is None and existing is not None:
        exchange_payload = existing.get("exchange_payload")
    strategy_name = incoming.get("strategy_name")
    if strategy_name is None and existing is not None:
        strategy_name = existing.get("strategy_name")

    return {
        "timestamp": _serialize_datetime(timestamp),
        "order_id": incoming["order_id"],
        "strategy_name": strategy_name,
        "market_id": incoming["market_id"],
        "status": merged_status,
        "requested_size": round(requested_size, 6),
        "limit_price": round(limit_price, 6) if limit_price is not None else None,
        "filled_size": round(filled_size, 6),
        "last_seen_status": _normalize_order_status(str(incoming.get("last_seen_status") or incoming_status)),
        "last_seen_at": _serialize_datetime(timestamp),
        "exchange_payload": _serialize_json_payload(exchange_payload),
    }


def _normalize_order_status(value: str) -> str:
    normalized = value.strip().upper()
    aliases = {
        "CANCELED": "CANCELLED",
        "MATCHED": "FILLED",
        "OPEN": "OPEN",
        "LIVE": "OPEN",
        "LIVE_RESTING": "LIVE_RESTING",
    }
    return aliases.get(normalized, normalized)


def _order_status_rank(value: str) -> int:
    ranks = {
        "PENDING": 0,
        "OPEN": 1,
        "PARTIALLY_FILLED": 2,
        "FILLED": 3,
        "CANCELLED": 4,
        "REJECTED": 4,
        "EXPIRED": 4,
    }
    return ranks.get(_normalize_order_status(value), 1)


def _serialize_json_payload(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _coerce_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError("Expected datetime-like value")


def _utc_day(value: datetime) -> date:
    return value.astimezone(UTC).date()


def _iso_week(value: datetime) -> tuple[int, int]:
    iso = value.astimezone(UTC).isocalendar()
    return iso.year, iso.week


def _consecutive_failures(trades: tuple[Trade, ...]) -> int:
    failures = 0
    for trade in reversed(trades):
        if trade.outcome is TradeOutcome.WIN:
            break
        if trade.outcome in {TradeOutcome.LOSS, TradeOutcome.CANCELLED}:
            failures += 1
            continue
        break
    return failures


def _score_band(score: float) -> str | None:
    if score < 7.0:
        return None
    if score < 8.0:
        return "7.0-7.99"
    if score < 9.0:
        return "8.0-8.99"
    return "9.0-10.0"
