from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_environment
from bot.tracker import TradeTracker


EXPORT_COLUMNS = (
    "id",
    "timestamp",
    "strategy_name",
    "market_id",
    "market_question",
    "category",
    "side",
    "entry_price",
    "screened_price",
    "position_size",
    "score",
    "hours_to_close",
    "order_id",
    "fill_price",
    "fill_slippage",
    "fill_slippage_pct",
    "fill_time",
    "outcome",
    "resolution_price",
    "pnl",
    "execution_fee",
    "paper_trade",
    "notes",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export tracked trades from SQLite as JSON or CSV.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--strategy-name", default=None)
    return parser.parse_args()


def fetch_trade_export_rows(
    tracker: TradeTracker,
    *,
    limit: int,
    strategy_name: str | None = None,
) -> list[dict[str, object]]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    params: dict[str, object] = {"limit": limit}
    where_clause = ""
    if strategy_name is not None:
        params["strategy_name"] = strategy_name
        where_clause = "WHERE strategy_name = :strategy_name"
    with tracker.connection() as conn:
        rows = conn.execute(
            """
            SELECT
              id,
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
              paper_trade,
              notes
            FROM trades
            {where_clause}
            ORDER BY timestamp DESC, id DESC
            LIMIT :limit
            """.format(where_clause=where_clause),
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def render_trade_export(rows: list[dict[str, object]], *, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(rows, indent=2, default=str)
    if fmt == "csv":
        return render_trade_export_csv(rows)
    raise ValueError(f"Unsupported export format: {fmt}")


def render_trade_export_csv(rows: list[dict[str, object]]) -> str:
    from io import StringIO

    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(EXPORT_COLUMNS))
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column) for column in EXPORT_COLUMNS})
    return buffer.getvalue()


def main() -> None:
    args = parse_args()
    env = load_environment()
    tracker = TradeTracker(env.database_url)
    tracker.initialize()
    rows = fetch_trade_export_rows(tracker, limit=args.limit, strategy_name=args.strategy_name)
    rendered = render_trade_export(rows, fmt=args.format)
    if args.output is not None:
        args.output.write_text(rendered)
        print(f"exported {len(rows)} trades to {args.output}")
        return
    print(rendered)


if __name__ == "__main__":
    main()
